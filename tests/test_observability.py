from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import asyncio
import sys

import pytest

from backend.mcp_servers import researcher_mcp_servers, trader_mcp_servers
from backend.mcp_servers import ObservedMCPServerStdio
from backend.observability import (
    BudgetExceeded,
    BudgetHooks,
    CycleBudget,
    CycleContext,
    TelemetryRepository,
    safe_error,
)


def market_status() -> dict:
    return {
        "mode": "simulated",
        "degraded": False,
        "freshness_threshold_seconds": 300,
        "last_successful_observation": None,
    }


def test_mcp_servers_have_stable_attributable_names() -> None:
    assert [server.name for server in trader_mcp_servers()] == ["notifications", "market-data"]
    assert [server.name for server in researcher_mcp_servers("Alice")] == [
        "research-fetch", "research-search", "memory-alice"
    ]


def test_diagnostics_are_redacted_and_bounded() -> None:
    diagnostic = safe_error("authorization=Bearer-secret sk-abcdefghijklmnopqrstuvwxyz " + "x" * 600)
    assert "Bearer-secret" not in diagnostic
    assert "abcdefghijklmnopqrstuvwxyz" not in diagnostic
    assert "[REDACTED]" in diagnostic
    assert len(diagnostic) == 500


def test_service_transitions_and_circuit_breaker_are_persisted(tmp_path) -> None:
    repository = TelemetryRepository(str(tmp_path / "health.db"))
    repository.register_service("test-service", True)
    repository.mark_starting("test-service")
    repository.mark_failure("test-service", "api_key=do-not-store", threshold=2, reset_seconds=60)
    first = repository.service("test-service")
    assert first.state == "degraded"
    assert first.error_summary == "api_key=[REDACTED]"
    repository.mark_failure("test-service", "timeout", threshold=2, reset_seconds=60)
    assert repository.service("test-service").state == "unavailable"
    assert repository.circuit_is_open("test-service")
    repository.mark_success("test-service", 12.5)
    recovered = repository.service("test-service")
    assert recovered.state == "healthy"
    assert recovered.consecutive_failures == 0
    assert recovered.failure_count == 2
    assert recovered.attempt_count == 3


def test_cycle_usage_cost_and_decision_trace_metadata_round_trip(tmp_path) -> None:
    repository = TelemetryRepository(str(tmp_path / "cycles.db"))
    context = CycleContext.create(run_id="run-1", scenario_id="scenario-1")
    budget = CycleBudget(
        max_turns=4, max_tokens=1000, max_wall_seconds=10, max_spend_usd=Decimal("1"),
        input_cost_per_million=Decimal("2"), output_cost_per_million=Decimal("4"),
    )
    repository.start_cycle(context, "Alice", "test-model", "trader-v1", "simulated", budget)
    usage = SimpleNamespace(requests=2, input_tokens=100, output_tokens=50, total_tokens=150)
    cost = budget.estimate_cost(usage.input_tokens, usage.output_tokens)
    repository.finish_cycle(
        context.cycle_id, status="succeeded", usage=usage, latency_ms=25,
        estimated_cost=cost, decision_ids=["decision-1"], trace_id="trace-test",
    )
    metadata = repository.decision_metadata("decision-1")
    assert metadata["cycle_id"] == context.cycle_id
    assert metadata["trace_id"] == "trace-test"
    assert metadata["total_tokens"] == 150
    assert Decimal(metadata["estimated_cost_usd"]) == Decimal("0.0004")
    payload = repository.health_payload(market_status())
    assert payload["current_cycle_id"] is None
    assert payload["metrics"]["cycle_success_rate"] == 1


def test_budget_hook_stops_before_an_over_budget_model_request() -> None:
    budget = CycleBudget(max_tokens=10, max_spend_usd=Decimal("1"))
    context = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=8, output_tokens=2, total_tokens=10)
    )
    with pytest.raises(BudgetExceeded, match="token budget"):
        asyncio.run(BudgetHooks(budget).on_llm_start(context, None, None, []))


def test_failed_mcp_startup_attributes_redacted_subprocess_diagnostic(tmp_path, monkeypatch) -> None:
    import backend.mcp_servers as servers

    monkeypatch.setattr(servers, "RETRIES", 0)
    repository = TelemetryRepository(str(tmp_path / "startup.db"))
    server = ObservedMCPServerStdio(
        {
            "command": sys.executable,
            "args": ["-c", "import sys; print('api_key=never-store-this', file=sys.stderr)"],
        },
        name="broken-test-service",
    )
    server.telemetry = repository
    repository.register_service(server.name, True)
    with pytest.raises(Exception):
        asyncio.run(server.connect())
    health = repository.service(server.name)
    assert health.state == "degraded"
    assert "never-store-this" not in health.error_summary
    assert "[REDACTED]" in health.error_summary
