"""Product-facing replay and experiment services.

The services expose the existing point-in-time evaluator without weakening its
outcome boundary. Replay decisions and reveals are persisted for auditability;
all results remain offline paper-trading evaluation artifacts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from backend import database
from evals.fixtures import FixtureSet, load_dataset
from evals.runner import run_evaluation
from evals.strategies import default_strategies


DATASET_ROOT = Path("evals/datasets/historical-v1")
RESULTS_ROOT = Path("evals/results")


class ProductModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayRequest(ProductModel):
    scenario_id: str
    strategy: str = "multi_agent"
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class ExperimentRequest(ProductModel):
    model: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


def _strategy_map():
    return {strategy.name: strategy for strategy in default_strategies()}


class ProductService:
    def __init__(
        self,
        db_path: str | None = None,
        dataset_root: Path = DATASET_ROOT,
        results_root: Path = RESULTS_ROOT,
    ):
        self.db_path = db_path or database.DB
        database.initialize_database(self.db_path)
        self.dataset_root = dataset_root
        self.results_root = results_root

    def _fixtures(self) -> FixtureSet:
        return load_dataset(self.dataset_root)

    def scenarios(self) -> dict:
        fixtures = self._fixtures()
        return {
            "dataset": fixtures.manifest.model_dump(mode="json"),
            "strategies": sorted(_strategy_map()),
            "scenarios": [
                {
                    "scenario_id": item.scenario_id,
                    "decision_at": item.decision_at.isoformat(),
                    "market_timestamp": item.market_timestamp.isoformat(),
                    "retrieved_at": item.retrieved_at.isoformat(),
                    "symbols": sorted(item.prices),
                    "benchmark_symbol": item.benchmark_symbol,
                    "source_count": len(item.sources),
                    "outcome_available": False,
                }
                for item in fixtures.decisions
            ],
            "notice": "Outcome data is withheld until a replay decision is complete.",
        }

    def create_replay(self, request: ReplayRequest) -> dict:
        fixtures = self._fixtures()
        fixture = next(
            (item for item in fixtures.decisions if item.scenario_id == request.scenario_id), None
        )
        if fixture is None:
            raise KeyError(f"unknown scenario {request.scenario_id}")
        strategy = _strategy_map().get(request.strategy)
        if strategy is None:
            raise KeyError(f"unknown strategy {request.strategy}")
        decision = strategy.decide(fixture, request.seed)
        replay_id = str(
            uuid5(
                NAMESPACE_URL,
                f"replay:{fixtures.manifest.dataset_version}:{request.scenario_id}:"
                f"{request.strategy}:{request.seed}",
            )
        )
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT 1 FROM replay_sessions WHERE replay_id=?", (replay_id,)
            ).fetchone()
        if existing:
            return self.replay(replay_id)
        payload = {
            "replay_id": replay_id,
            "scenario_id": fixture.scenario_id,
            "strategy": strategy.name,
            "seed": request.seed,
            "status": "decision_complete",
            "decision_at": fixture.decision_at.isoformat(),
            "market_timestamp": fixture.market_timestamp.isoformat(),
            "market_retrieved_at": fixture.retrieved_at.isoformat(),
            "inputs": {
                "prices": {key: str(value) for key, value in fixture.prices.items()},
                "trailing_returns": {
                    key: str(value) for key, value in fixture.trailing_returns.items()
                },
                "sources": [source.model_dump(mode="json") for source in fixture.sources],
            },
            "decision": decision.model_dump(mode="json"),
            "outcome": None,
            "paper_trading_only": True,
        }
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO replay_sessions
                   (replay_id, scenario_id, strategy, seed, status, decision_payload,
                    outcome_payload, created_at, revealed_at)
                   VALUES (?, ?, ?, ?, 'decision_complete', ?, NULL, ?, NULL)""",
                (
                    replay_id,
                    fixture.scenario_id,
                    strategy.name,
                    request.seed,
                    json.dumps(payload, default=str),
                    now,
                ),
            )
        return payload

    def replay(self, replay_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT decision_payload, outcome_payload, status FROM replay_sessions WHERE replay_id=?",
                (replay_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown replay {replay_id}")
        payload = json.loads(row[0])
        payload["status"] = row[2]
        payload["outcome"] = json.loads(row[1]) if row[1] else None
        return payload

    def reveal(self, replay_id: str) -> dict:
        session = self.replay(replay_id)
        if session["outcome"] is not None:
            return session
        fixtures = self._fixtures()
        fixture = next(
            item for item in fixtures.decisions if item.scenario_id == session["scenario_id"]
        )
        outcome = fixtures.reveal_outcome(fixture.scenario_id, fixture.decision_at)
        weights = {
            symbol: Decimal(str(weight))
            for symbol, weight in session["decision"]["weights"].items()
        }
        portfolio_return = sum(
            weight * ((outcome.prices[symbol] / fixture.prices[symbol]) - Decimal("1"))
            for symbol, weight in weights.items()
        )
        benchmark_return = (
            outcome.prices[fixture.benchmark_symbol] / fixture.prices[fixture.benchmark_symbol]
        ) - Decimal("1")
        result = {
            "outcome_at": outcome.outcome_at.isoformat(),
            "prices": {key: str(value) for key, value in outcome.prices.items()},
            "portfolio_return": str(portfolio_return),
            "benchmark_return": str(benchmark_return),
            "benchmark_relative_return": str(portfolio_return - benchmark_return),
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE replay_sessions SET status='outcome_revealed', outcome_payload=?,
                   revealed_at=? WHERE replay_id=? AND status='decision_complete'""",
                (json.dumps(result), datetime.now(timezone.utc).isoformat(), replay_id),
            )
        return self.replay(replay_id)

    def run_experiment(self, request: ExperimentRequest) -> dict:
        report = run_evaluation(
            self._fixtures(),
            self.results_root,
            seed=request.seed,
            model=request.model,
            prompt_version=request.prompt_version,
        )
        return report.model_dump(mode="json")

    def experiments(self) -> list[dict]:
        if not self.results_root.exists():
            return []
        reports = []
        for path in self.results_root.glob("*/report.json"):
            try:
                reports.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(
            reports,
            key=lambda item: item.get("metadata", {}).get("completed_at", ""),
            reverse=True,
        )
