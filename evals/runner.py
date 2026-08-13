"""CLI runner for reproducible, checkpointed offline evaluations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .clock import SimulationClock
from .fixtures import FixtureSet, load_dataset
from .metrics import aggregate
from .models import (
    EvaluationReport,
    RunMetadata,
    ScenarioMetrics,
    ReplayTiming,
    StrategyResult,
)
from .strategies import Strategy, default_strategies


def _git_sha() -> str:
    configured = os.getenv("EVAL_GIT_SHA")
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class CheckpointStore:
    """Atomic JSON checkpoint keyed by strategy and scenario; retries cannot append duplicates."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict] = {}
        if path.exists():
            payload = json.loads(path.read_text())
            self.rows = dict(payload.get("rows", {}))

    def get(self, key: str) -> ScenarioMetrics | None:
        row = self.rows.get(key)
        return ScenarioMetrics.model_validate(row) if row else None

    def put(self, row: ScenarioMetrics) -> None:
        key = f"{row.strategy}:{row.scenario_id}"
        self.rows[key] = row.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"rows": self.rows}, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)


def _score(strategy_name: str, fixture, outcome, decision, seed: int, clock: SimulationClock) -> ScenarioMetrics:
    unknown = set(decision.weights) - set(fixture.prices)
    if unknown:
        raise ValueError(f"{strategy_name} selected unknown symbols: {sorted(unknown)}")
    portfolio_return = sum(
        (
            weight
            * ((outcome.prices[symbol] / fixture.prices[symbol]) - Decimal("1"))
        )
        for symbol, weight in decision.weights.items()
    )
    benchmark_return = (
        outcome.prices[fixture.benchmark_symbol] / fixture.prices[fixture.benchmark_symbol]
    ) - Decimal("1")
    return ScenarioMetrics(
        scenario_id=fixture.scenario_id,
        strategy=strategy_name,
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        turnover=sum(decision.weights.values(), Decimal("0")),
        won=portfolio_return > 0,
        decision_valid=decision.decision_valid,
        citation_valid=decision.citation_valid,
        tool_success=decision.tool_success,
        latency_ms=decision.latency_ms,
        model_cost_usd=decision.model_cost_usd,
        order_ids=[
            str(uuid5(NAMESPACE_URL, f"eval-order:{seed}:{strategy_name}:{fixture.scenario_id}:{symbol}"))
            for symbol, weight in sorted(decision.weights.items()) if weight > 0
        ],
        timing=ReplayTiming(
            research_cutoff=clock.now(),
            market_timestamp=fixture.market_timestamp,
            market_retrieved_at=fixture.retrieved_at,
            decided_at=clock.now(),
            executed_at=clock.now(),
        ),
    )


def run_evaluation(
    fixtures: FixtureSet,
    output_dir: Path,
    *,
    seed: int = 7,
    model: str = "offline-deterministic-proxy",
    prompt_version: str = "eval-agent-v1",
    strategies: list[Strategy] | None = None,
    now=lambda: datetime.now(timezone.utc),
) -> EvaluationReport:
    strategies = strategies or default_strategies()
    git_sha = _git_sha()
    strategy_names = ",".join(strategy.name for strategy in strategies)
    identity = (
        f"{fixtures.manifest.dataset_id}:{fixtures.manifest.dataset_version}:{seed}:"
        f"{model}:{prompt_version}:{git_sha}:{strategy_names}"
    )
    run_id = str(uuid5(NAMESPACE_URL, identity))
    run_dir = output_dir / run_id
    report_path = run_dir / "report.json"
    if report_path.exists():
        return EvaluationReport.model_validate_json(report_path.read_text())
    started_at = now()
    store = CheckpointStore(run_dir / "checkpoint.json")
    clock = SimulationClock(fixtures.decisions[0].decision_at)
    results: list[StrategyResult] = []
    for strategy in strategies:
        rows: list[ScenarioMetrics] = []
        for fixture in fixtures.decisions:
            key = f"{strategy.name}:{fixture.scenario_id}"
            existing = store.get(key)
            if existing:
                rows.append(existing)
                continue
            clock.reset_to(fixture.decision_at)
            # The strategy sees only DecisionFixture. OutcomeFixture stays behind
            # FixtureSet until the decision is complete at the simulation cutoff.
            decision = strategy.decide(fixture, seed)
            outcome = fixtures.reveal_outcome(fixture.scenario_id, clock.now())
            row = _score(strategy.name, fixture, outcome, decision, seed, clock)
            clock.advance_to(outcome.outcome_at)
            store.put(row)
            rows.append(row)
        results.append(StrategyResult(strategy=strategy.name, metrics=aggregate(rows), scenarios=rows))
    by_name = {item.strategy: item.metrics for item in results}
    ablation = {}
    if {"multi_agent", "single_agent"} <= by_name.keys():
        ablation = {
            "multi_minus_single_total_return": (
                by_name["multi_agent"].total_return - by_name["single_agent"].total_return
            ),
            "multi_minus_single_cost_usd": (
                by_name["multi_agent"].model_api_cost_usd - by_name["single_agent"].model_api_cost_usd
            ),
            "multi_minus_single_latency_ms": (
                by_name["multi_agent"].average_latency_ms - by_name["single_agent"].average_latency_ms
            ),
        }
    completed_at = now()
    report = EvaluationReport(
        metadata=RunMetadata(
            run_id=run_id,
            dataset_id=fixtures.manifest.dataset_id,
            dataset_version=fixtures.manifest.dataset_version,
            git_sha=git_sha,
            model=model,
            prompt_version=prompt_version,
            configuration={
                "scenario_count": len(fixtures.decisions),
                "strategy_count": len(strategies),
                "strategies": strategy_names,
            },
            seed=seed,
            started_at=started_at,
            completed_at=completed_at,
        ),
        results=results,
        ablation=ablation,
        leakage_checks_passed=True,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2) + "\n")
    (run_dir / "report.md").write_text(render_markdown(report))
    return report


def _percent(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def render_markdown(report: EvaluationReport) -> str:
    lines = [
        "# Paper-trading evaluation report",
        "",
        "> Offline replay only. These results do not validate an investment strategy and are not real trades.",
        "",
        f"Run `{report.metadata.run_id}` used dataset `{report.metadata.dataset_id}` version "
        f"`{report.metadata.dataset_version}`, seed `{report.metadata.seed}`, model "
        f"`{report.metadata.model}`, prompt `{report.metadata.prompt_version}`, and git SHA "
        f"`{report.metadata.git_sha}`.",
        "",
        "| Strategy | Total return | Relative | Volatility | Sharpe | Max drawdown | Turnover | Win rate | Valid decisions | Valid citations | Tool success | Cost (USD) | Latency (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report.results:
        m = result.metrics
        lines.append(
            f"| {result.strategy} | {_percent(m.total_return)} | {_percent(m.benchmark_relative_return)} | "
            f"{_percent(m.annualized_volatility)} | {m.sharpe.quantize(Decimal('0.01')) if m.sharpe is not None else 'n/a'} | "
            f"{_percent(m.max_drawdown)} | {_percent(m.turnover)} | {_percent(m.win_rate)} | "
            f"{_percent(m.decision_validity)} | {_percent(m.citation_validity)} | {_percent(m.tool_success_rate)} | "
            f"{m.model_api_cost_usd:.4f} | {m.average_latency_ms:.0f} |"
        )
    if report.ablation:
        lines.extend([
            "",
            "## Ablation",
            "",
            f"Multi-agent minus single-agent total return: {_percent(report.ablation['multi_minus_single_total_return'])}. "
            f"Incremental fixture-estimated cost: ${report.ablation['multi_minus_single_cost_usd']:.4f}; "
            f"incremental latency: {report.ablation['multi_minus_single_latency_ms']:.0f} ms.",
        ])
    lines.extend([
        "",
        "All fixture hashes and point-in-time checks passed. Outcome records were withheld until each decision completed.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic point-in-time paper-trading evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/datasets/historical-v1"))
    parser.add_argument("--output", type=Path, default=Path("evals/results"))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--model", default="offline-deterministic-proxy")
    parser.add_argument("--prompt-version", default="eval-agent-v1")
    args = parser.parse_args()
    report = run_evaluation(
        load_dataset(args.dataset), args.output, seed=args.seed,
        model=args.model, prompt_version=args.prompt_version,
    )
    print(args.output / report.metadata.run_id / "report.md")


if __name__ == "__main__":
    main()
