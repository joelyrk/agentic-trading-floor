import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.agent_runs import AgentRunRepository
from backend.decisions import (
    DecisionPipeline,
    EvidenceClaim,
    ExecutionService,
    ProposalService,
    ResearchBrief,
    RiskEngine,
    RiskPolicy,
    RiskService,
    SourceRecord,
    TradingDecision,
)
from backend.decisions.models import ProposedTrade
from backend.decisions.repository import DecisionRepository
from backend.market.models import DataMode, MarketObservation, ObservationSource
from backend.notifications import (
    NotificationConflict,
    NotificationDispatcher,
    NotificationRepository,
    enqueue_run_notifications,
)
from backend.observability import CycleBudget, CycleContext, TelemetryRepository
from backend.push_server import PushModelArgs

NOW = datetime(2026, 8, 22, 9, tzinfo=timezone.utc)


def observation(symbol: str = "SPY") -> MarketObservation:
    return MarketObservation(
        symbol=symbol,
        price=Decimal("100"),
        currency="USD",
        market_timestamp=NOW,
        retrieved_at=NOW,
        source=ObservationSource.SIMULATOR,
        mode=DataMode.SIMULATED,
        is_stale=False,
        provider_endpoint="test/v1",
    )


def create_run(path: str):
    runs = AgentRunRepository(path)
    run, _ = runs.request(
        trigger="manual",
        requested_by="test",
        idempotency_key="notification-test",
        observation=observation(),
    )
    runs.mark_running(run.run_id)
    return runs, run


def test_notification_subprocess_receives_only_its_provider_credentials(monkeypatch) -> None:
    import backend.mcp_servers as servers

    monkeypatch.setenv("PUSHOVER_USER", "test-user")
    monkeypatch.setenv("PUSHOVER_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")

    notification = servers.notification_mcp_server()

    assert notification.params.env["PUSHOVER_USER"] == "test-user"
    assert notification.params.env["PUSHOVER_TOKEN"] == "test-token"
    assert "OPENAI_API_KEY" not in notification.params.env
    assert [server.name for server in servers.trader_mcp_servers()] == ["market-data"]


def test_disabled_notification_is_reported_as_an_error(monkeypatch) -> None:
    import backend.push_server as push_server

    monkeypatch.setattr(push_server, "pushover_user", None)
    monkeypatch.setattr(push_server, "pushover_token", None)

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        push_server.push(PushModelArgs(message="Pending paper proposal"))


def test_notification_posts_to_pushover(monkeypatch) -> None:
    import backend.push_server as push_server

    monkeypatch.setattr(push_server, "pushover_user", "test-user")
    monkeypatch.setattr(push_server, "pushover_token", "test-token")
    observed = {}

    class Response:
        def raise_for_status(self) -> None:
            observed["status_checked"] = True

    def fake_post(url, *, data, timeout):
        observed.update(url=url, data=data, timeout=timeout)
        return Response()

    monkeypatch.setattr(push_server.requests, "post", fake_post)

    result = push_server.push(PushModelArgs(message="Pending paper proposal"))

    assert result == "Push notification sent"
    assert observed == {
        "url": "https://api.pushover.net/1/messages.json",
        "data": {
            "user": "test-user",
            "token": "test-token",
            "message": "Pending paper proposal",
        },
        "timeout": 10,
        "status_checked": True,
    }


def test_run_summary_is_durable_and_idempotent(tmp_path) -> None:
    path = str(tmp_path / "notifications.db")
    runs, run = create_run(path)
    telemetry = TelemetryRepository(path)
    for name, status in (("warren", "succeeded"), ("cathie", "failed")):
        context = CycleContext.create(run_id=run.run_id)
        telemetry.start_cycle(context, name, "model", "prompt", "simulated", CycleBudget())
        telemetry.finish_cycle(
            context.cycle_id,
            status=status,
            usage=None,
            latency_ms=1,
            estimated_cost=Decimal("0"),
            error="invalid output" if status == "failed" else None,
        )
    record = runs.finish(run.run_id, "partial_success", "invalid output")

    first = enqueue_run_notifications(record, path)
    second = enqueue_run_notifications(record, path)

    assert len(first) == len(second) == 1
    assert first[0].event_key == f"run:{run.run_id}:summary"
    assert first[0].status == "pending"
    assert "1 succeeded, 1 unsuccessful (Cathie)" in first[0].message
    with pytest.raises(NotificationConflict, match="different content"):
        NotificationRepository(path).enqueue(
            event_key=first[0].event_key,
            event_type="run_summary",
            run_id=run.run_id,
            message="different summary",
        )


def test_policy_outcome_notification_uses_persisted_decision(tmp_path) -> None:
    path = str(tmp_path / "notifications.db")
    runs, run = create_run(path)
    decisions = DecisionRepository(path)
    source = SourceRecord(
        source_id="s1",
        canonical_url="https://example.com/story",
        publisher="Example",
        title="Story",
        published_at=NOW,
        retrieved_at=NOW,
        supporting_excerpt="A bounded test excerpt.",
    )
    claim = EvidenceClaim(
        claim_id="c1",
        claim="A supported material claim.",
        source_ids=["s1"],
        stance="supports",
        confidence=Decimal("0.8"),
    )
    output = TradingDecision(
        research=ResearchBrief(summary="summary", as_of=NOW, sources=[source], claims=[claim]),
        appraisal="test appraisal",
        proposals=[
            ProposedTrade(
                symbol="AAPL",
                side="buy",
                quantity=2,
                sector="technology",
                rationale="test rationale",
                evidence_claim_ids=["c1"],
            )
        ],
    )
    policy = RiskPolicy(
        max_position_percentage=Decimal("1"),
        max_symbol_concentration=Decimal("1"),
        max_sector_concentration=Decimal("1"),
        minimum_cash_reserve=Decimal("0"),
        maximum_order_notional=Decimal("100000"),
        maximum_daily_turnover=Decimal("100000"),
        maximum_drawdown=Decimal("1"),
    )
    proposal, decision, execution = DecisionPipeline(
        ProposalService(decisions, observation, clock=lambda: NOW),
        RiskService(decisions, RiskEngine(policy), observation, clock=lambda: NOW),
        ExecutionService(decisions, clock=lambda: NOW),
    ).process("warren", output)[0]
    assert execution is not None

    telemetry = TelemetryRepository(path)
    context = CycleContext.create(run_id=run.run_id)
    telemetry.start_cycle(context, "warren", "model", "prompt", "simulated", CycleBudget())
    telemetry.finish_cycle(
        context.cycle_id,
        status="succeeded",
        usage=None,
        latency_ms=1,
        estimated_cost=Decimal("0"),
        decision_ids=[str(decision.decision_id)],
    )
    record = runs.finish(run.run_id, "succeeded")

    events = enqueue_run_notifications(record, path)

    assert [event.event_type for event in events] == ["decision_outcome", "run_summary"]
    assert events[0].decision_id == str(decision.decision_id)
    assert events[0].message == (
        f"Warren paper decision: BUY {proposal.quantity} AAPL "
        "approved by policy and paper-executed."
    )
    assert "1 policy decisions" in events[1].message


def test_dispatcher_retries_then_suppresses_duplicate_delivery(tmp_path) -> None:
    path = str(tmp_path / "notifications.db")
    runs, run = create_run(path)
    record = runs.finish(run.run_id, "failed", "test failure")
    enqueue_run_notifications(record, path)

    class Sender:
        def __init__(self):
            self.messages = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def send(self, message: str) -> None:
            self.messages.append(message)
            if len(self.messages) < 3:
                raise RuntimeError("temporary provider failure")

    sender = Sender()
    dispatcher = NotificationDispatcher(
        path,
        sender_factory=lambda: sender,
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    report = asyncio.run(dispatcher.dispatch_pending())
    duplicate = asyncio.run(dispatcher.dispatch_pending())
    event = NotificationRepository(path).for_run(run.run_id)[0]

    assert report.sent == 1
    assert report.failed == 0
    assert duplicate.sent == duplicate.failed == 0
    assert len(sender.messages) == 3
    assert event.status == "sent"
    assert event.attempts == 3
    assert event.sent_at is not None


def test_dispatcher_persists_failure_after_bounded_attempts(tmp_path) -> None:
    path = str(tmp_path / "notifications.db")
    runs, run = create_run(path)
    record = runs.finish(run.run_id, "failed", "test failure")
    enqueue_run_notifications(record, path)

    class Sender:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def send(self, message: str) -> None:
            raise RuntimeError("provider unavailable")

    dispatcher = NotificationDispatcher(
        path,
        sender_factory=Sender,
        max_attempts=2,
        retry_backoff_seconds=0,
    )

    report = asyncio.run(dispatcher.dispatch_pending())
    event = NotificationRepository(path).for_run(run.run_id)[0]

    assert report.sent == 0
    assert report.failed == 1
    assert event.status == "failed"
    assert event.attempts == 2
    assert event.sent_at is None
    assert event.error_summary == "provider unavailable"
