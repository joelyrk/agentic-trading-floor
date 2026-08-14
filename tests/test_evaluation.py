import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from evals.clock import SimulationClock
from evals.fixtures import FixtureIntegrityError, load_dataset
from evals.metrics import aggregate
from evals.models import ScenarioMetrics, StrategyDecision
from evals.runner import CheckpointStore, run_evaluation

DATASET = Path("evals/datasets/historical-v1")
NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def row(scenario_id: str, value: str, benchmark: str = "0") -> ScenarioMetrics:
    return ScenarioMetrics(
        scenario_id=scenario_id,
        strategy="known",
        portfolio_return=Decimal(value),
        benchmark_return=Decimal(benchmark),
        turnover=Decimal("1"),
        won=Decimal(value) > 0,
        decision_valid=True,
        citation_valid=True,
        tool_success=True,
        latency_ms=10,
        model_cost_usd=Decimal("0.01"),
    )


def test_historical_manifest_and_no_lookahead_boundaries() -> None:
    fixtures = load_dataset(DATASET)
    assert len(fixtures.decisions) == 30
    assert not hasattr(fixtures.decisions[0], "outcome_at")
    for decision in fixtures.decisions:
        assert decision.market_timestamp <= decision.retrieved_at <= decision.decision_at
        assert all(
            source.published_at <= source.retrieved_at <= decision.decision_at
            for source in decision.sources
        )
        outcome = fixtures.reveal_outcome(decision.scenario_id, decision.decision_at)
        assert outcome.outcome_at > decision.decision_at


def test_outcome_cannot_be_revealed_before_cutoff() -> None:
    fixtures = load_dataset(DATASET)
    decision = fixtures.decisions[0]
    with pytest.raises(FixtureIntegrityError, match="before the decision"):
        fixtures.reveal_outcome(
            decision.scenario_id, decision.decision_at - timedelta(microseconds=1)
        )


def test_fixture_hash_prevents_mutation(tmp_path) -> None:
    root = tmp_path / "dataset"
    root.mkdir()
    for source in DATASET.iterdir():
        if source.suffix in {".json", ".md"}:
            (root / source.name).write_bytes(source.read_bytes())
    decision_path = root / "decision_fixtures.json"
    decision_path.write_bytes(decision_path.read_bytes() + b" ")
    with pytest.raises(FixtureIntegrityError, match="hash mismatch"):
        load_dataset(root)


def test_simulation_clock_is_monotonic() -> None:
    clock = SimulationClock(NOW)
    clock.advance_to(NOW + timedelta(days=1))
    assert clock.now() == NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(NOW)


def test_metric_calculations_against_known_returns() -> None:
    metrics = aggregate([row("up", "0.10", "0.05"), row("down", "-0.10", "-0.05")])
    assert metrics.total_return == Decimal("-0.0100")
    assert metrics.benchmark_return == Decimal("-0.0025")
    assert metrics.benchmark_relative_return == Decimal("-0.0075")
    assert metrics.max_drawdown == Decimal("0.10")
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.turnover == Decimal("1")
    assert metrics.model_api_cost_usd == Decimal("0.02")
    assert metrics.annualized_volatility.quantize(Decimal("0.000001")) == Decimal("1.587451")


def test_replay_is_deterministic_and_generates_both_reports(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EVAL_GIT_SHA", "test-sha")
    fixtures = load_dataset(DATASET)
    first = run_evaluation(fixtures, tmp_path / "one", now=lambda: NOW)
    second = run_evaluation(fixtures, tmp_path / "two", now=lambda: NOW)
    assert first == second
    assert len(first.results) == 7
    assert all(len(result.scenarios) == 30 for result in first.results)
    order_ids = [
        order_id
        for result in first.results
        for scenario in result.scenarios
        for order_id in scenario.order_ids
    ]
    assert len(order_ids) == len(set(order_ids))
    assert all(
        scenario.timing.decided_at == scenario.timing.research_cutoff == scenario.timing.executed_at
        for result in first.results
        for scenario in result.scenarios
    )
    run_dir = tmp_path / "one" / first.metadata.run_id
    assert (run_dir / "report.json").exists()
    assert "not real trades" in (run_dir / "report.md").read_text()


def test_checkpoint_retry_replaces_same_order_key_without_duplicates(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.json")
    original = row("scenario-1", "0.01")
    retried = original.model_copy(update={"portfolio_return": Decimal("0.02")})
    store.put(original)
    store.put(retried)
    payload = json.loads((tmp_path / "checkpoint.json").read_text())
    assert list(payload["rows"]) == ["known:scenario-1"]
    assert store.get("known:scenario-1") == retried


def test_interrupted_run_resumes_without_repeating_scenarios_or_orders(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EVAL_GIT_SHA", "resume-sha")
    fixtures = load_dataset(DATASET)

    class Interrupting:
        name = "interrupt"
        calls = 0

        def decide(self, fixture, seed):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("interrupted")
            return StrategyDecision(weights={fixture.benchmark_symbol: Decimal("1")})

    with pytest.raises(RuntimeError, match="interrupted"):
        run_evaluation(fixtures, tmp_path, strategies=[Interrupting()], now=lambda: NOW)

    class Resuming:
        name = "interrupt"
        calls = 0

        def decide(self, fixture, seed):
            self.calls += 1
            return StrategyDecision(weights={fixture.benchmark_symbol: Decimal("1")})

    strategy = Resuming()
    report = run_evaluation(fixtures, tmp_path, strategies=[strategy], now=lambda: NOW)
    assert strategy.calls == 28
    rows = report.results[0].scenarios
    assert len(rows) == 30
    order_ids = [item.order_ids[0] for item in rows]
    assert len(order_ids) == len(set(order_ids)) == 30
