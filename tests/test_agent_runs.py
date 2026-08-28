import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.agent_runs import AgentRunConflict, AgentRunRepository, UnchangedMarketData
from backend.market.models import DataMode, MarketObservation, ObservationSource
from backend.observability import CycleBudget, CycleContext, TelemetryRepository

NOW = datetime(2026, 8, 15, 2, tzinfo=timezone.utc)


def observation(*, market_timestamp: datetime = NOW) -> MarketObservation:
    return MarketObservation(
        symbol="SPY",
        price=Decimal("650"),
        currency="USD",
        market_timestamp=market_timestamp,
        retrieved_at=market_timestamp + timedelta(minutes=5),
        source=ObservationSource.MASSIVE,
        mode=DataMode.END_OF_DAY,
        is_stale=False,
        provider_endpoint="v2/aggs/ticker/SPY/prev",
    )


def test_agent_run_reservation_is_idempotent_and_serialized(tmp_path) -> None:
    repository = AgentRunRepository(str(tmp_path / "runs.db"))
    first, created = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    assert created is True
    retry, created = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    assert created is False
    assert retry.run_id == first.run_id

    with pytest.raises(AgentRunConflict, match="already active"):
        repository.request(
            trigger="scheduled",
            requested_by="scheduler",
            idempotency_key="request-2",
            observation=observation(market_timestamp=NOW + timedelta(days=1)),
        )


def test_agent_run_refuses_an_already_consumed_market_snapshot(tmp_path) -> None:
    repository = AgentRunRepository(str(tmp_path / "runs.db"))
    run, _ = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    repository.mark_running(run.run_id)
    repository.finish(run.run_id, "failed", "bounded test failure")

    with pytest.raises(UnchangedMarketData, match="market data has not changed"):
        repository.request(
            trigger="manual",
            requested_by="local-console",
            idempotency_key="request-2",
            observation=observation(),
        )


def test_failed_proposal_free_manual_run_can_be_retried_with_a_new_audit_id(tmp_path) -> None:
    repository = AgentRunRepository(str(tmp_path / "runs.db"))
    run, _ = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    repository.mark_running(run.run_id)
    repository.finish(run.run_id, "failed", "model output failed before proposals")

    assert repository.retryability(run.run_id) == (True, None)
    retry = repository.retry(run.run_id, "retry-1")
    assert retry.run_id != run.run_id
    assert retry.retry_of == run.run_id
    assert retry.market_timestamp == run.market_timestamp
    assert retry.status == "queued"
    assert repository.retry(run.run_id, "retry-1").run_id == retry.run_id


def test_failed_proposal_free_scheduled_run_can_be_retried_as_manual(tmp_path) -> None:
    repository = AgentRunRepository(str(tmp_path / "runs.db"))
    run, _ = repository.request(
        trigger="scheduled",
        requested_by="scheduler",
        idempotency_key="scheduled-1",
        observation=observation(),
    )
    repository.mark_running(run.run_id)
    repository.finish(run.run_id, "failed", "model output failed before proposals")

    assert repository.retryability(run.run_id) == (True, None)

    retry = repository.retry(run.run_id, "retry-scheduled-1")

    assert retry.trigger == "manual"
    assert retry.retry_of == run.run_id
    assert retry.market_timestamp == run.market_timestamp
    assert retry.requested_by == "scheduler"
    assert retry.status == "queued"


def test_retry_is_blocked_after_any_successful_cycle(tmp_path) -> None:
    path = str(tmp_path / "runs.db")
    repository = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    repository.mark_running(run.run_id)
    context = CycleContext.create(run_id=run.run_id)
    telemetry.start_cycle(context, "warren", "model", "prompt", "end_of_day", CycleBudget())
    telemetry.finish_cycle(
        context.cycle_id,
        status="succeeded",
        usage=None,
        latency_ms=1,
        estimated_cost=Decimal("0"),
    )
    repository.finish(run.run_id, "failed", "later agent failed")

    can_retry, reason = repository.retryability(run.run_id)
    assert can_retry is False
    assert "paper decisions" in reason
    with pytest.raises(AgentRunConflict, match="paper decisions"):
        repository.retry(run.run_id, "retry-1")


def test_agent_run_outcome_preserves_successes_when_one_trader_fails(tmp_path) -> None:
    path = str(tmp_path / "runs.db")
    runs = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = runs.request(
        trigger="scheduled",
        requested_by="scheduler",
        idempotency_key="scheduled-1",
        observation=observation(),
    )
    for index in range(4):
        context = CycleContext.create(run_id=run.run_id)
        telemetry.start_cycle(
            context, f"Trader {index}", "model", "prompt", "end_of_day", CycleBudget()
        )
        telemetry.finish_cycle(
            context.cycle_id,
            status="succeeded" if index < 3 else "failed",
            usage=None,
            latency_ms=1,
            estimated_cost=Decimal("0"),
            error="fourth trader failed" if index == 3 else None,
        )
    status, error = runs.cycle_outcome(run.run_id, 4)
    assert status == "partial_success"
    assert error == "fourth trader failed"


def test_agent_run_outcome_is_partial_when_proposal_was_skipped(tmp_path) -> None:
    path = str(tmp_path / "runs.db")
    runs = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = runs.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="warning-run",
        observation=observation(),
    )
    for index in range(4):
        context = CycleContext.create(run_id=run.run_id)
        telemetry.start_cycle(
            context, f"Trader {index}", "model", "prompt", "end_of_day", CycleBudget()
        )
        telemetry.finish_cycle(
            context.cycle_id,
            status="succeeded",
            usage=None,
            latency_ms=1,
            estimated_cost=Decimal("0"),
            error=("ALKEM: market_data_unavailable: empty_market_day" if index == 2 else None),
        )

    status, error = runs.cycle_outcome(run.run_id, 4)
    assert status == "partial_success"
    assert error == "ALKEM: market_data_unavailable: empty_market_day"


def test_agent_run_progress_correlates_stage_logs_and_pending_agents(tmp_path) -> None:
    path = str(tmp_path / "runs.db")
    runs = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = runs.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-progress",
        observation=observation(),
    )
    runs.mark_running(run.run_id)
    context = CycleContext.create(run_id=run.run_id)
    telemetry.start_cycle(context, "warren", "model", "prompt", "end_of_day", CycleBudget())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO logs(name, datetime, type, message) VALUES (?, datetime('now'), ?, ?)",
            ("warren", "cycle", f"Run {run.run_id}: synthesizing evidence from 5 sources"),
        )
        conn.execute(
            "INSERT INTO logs(name, datetime, type, message) VALUES (?, datetime('now'), ?, ?)",
            ("warren", "cycle", "Run another-run: unrelated"),
        )

    progress = runs.progress(run.run_id)
    assert progress is not None
    assert [agent.name for agent in progress.agents] == ["warren", "george", "ray", "cathie"]
    assert progress.agents[0].status == "running"
    assert progress.agents[0].current_activity == "synthesizing evidence from 5 sources"
    assert len(progress.agents[0].logs) == 1
    assert progress.agents[1].status == "pending"
    assert "Waiting" in progress.agents[1].current_activity


def test_agent_run_progress_exposes_unavailable_token_usage(tmp_path) -> None:
    path = str(tmp_path / "runs.db")
    runs = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = runs.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="usage-progress",
        observation=observation(),
    )
    context = CycleContext.create(run_id=run.run_id)
    telemetry.start_cycle(context, "warren", "model", "prompt", "end_of_day", CycleBudget())
    telemetry.finish_cycle(
        context.cycle_id,
        status="failed",
        usage=SimpleNamespace(requests=2, input_tokens=0, output_tokens=0, total_tokens=0),
        latency_ms=1,
        estimated_cost=Decimal("0"),
        error="incomplete_output",
    )

    progress = runs.progress(run.run_id)

    assert progress is not None
    assert progress.agents[0].usage_status == "unavailable"


def test_stale_active_agent_run_is_recovered(tmp_path) -> None:
    repository = AgentRunRepository(str(tmp_path / "runs.db"))
    run, _ = repository.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    with sqlite3.connect(repository.path) as conn:
        conn.execute(
            "UPDATE agent_runs SET requested_at=? WHERE run_id=?",
            (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(), run.run_id),
        )
    assert repository.recover_stale(timedelta(minutes=20)) == 1
    assert repository.get(run.run_id).status == "interrupted"


def test_reserved_run_executes_all_cycles_under_one_run_id(tmp_path, monkeypatch) -> None:
    import asyncio

    import backend.trading_floor as floor

    path = str(tmp_path / "runs.db")
    runs = AgentRunRepository(path)
    telemetry = TelemetryRepository(path)
    run, _ = runs.request(
        trigger="manual",
        requested_by="local-console",
        idempotency_key="request-1",
        observation=observation(),
    )
    observed_run_ids = []

    with sqlite3.connect(path) as conn:
        for name in ("warren", "george", "ray", "cathie"):
            conn.execute(
                "INSERT INTO accounts(name, account) VALUES (?, ?)",
                (
                    name,
                    json.dumps(
                        {
                            "name": name,
                            "balance": 10_000,
                            "strategy": "test",
                            "holdings": {},
                            "transactions": [],
                            "portfolio_value_time_series": [],
                        }
                    ),
                ),
            )

    class FakeTrader:
        def __init__(self, index: int):
            self.index = index
            self.name = ("Warren", "George", "Ray", "Cathie")[index]

        async def run(self, *, run_id: str):
            observed_run_ids.append(run_id)
            context = CycleContext.create(run_id=run_id)
            telemetry.start_cycle(
                context,
                f"Trader {self.index}",
                "model",
                "prompt",
                "end_of_day",
                CycleBudget(),
            )
            telemetry.finish_cycle(
                context.cycle_id,
                status="succeeded",
                usage=None,
                latency_ms=1,
                estimated_cost=Decimal("0"),
            )

    monkeypatch.setattr(floor, "ensure_trace_processor", lambda: None)
    published = []

    async def publish(record, path):
        published.append((record.run_id, path))
        raise RuntimeError("notification provider unavailable")

    monkeypatch.setattr(floor, "publish_run_notifications", publish)
    result = asyncio.run(
        floor.execute_agent_run(
            run.run_id,
            repository=runs,
            traders=[FakeTrader(index) for index in range(4)],
            max_concurrency=1,
        )
    )
    assert result.status == "succeeded"
    assert observed_run_ids == [run.run_id] * 4
    assert published == [(run.run_id, path)]
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT account FROM accounts ORDER BY name").fetchall()
    assert all(len(json.loads(row[0])["portfolio_value_time_series"]) == 2 for row in rows)
