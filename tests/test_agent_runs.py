import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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


def test_agent_run_outcome_requires_every_trader_cycle_to_succeed(tmp_path) -> None:
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
    assert status == "failed"
    assert error == "fourth trader failed"


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

    class FakeTrader:
        def __init__(self, index: int):
            self.index = index

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
