"""Deterministic, durable delivery of audited paper-trading notifications."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from backend import database
from backend.agent_runs import AgentRunRecord
from backend.decisions import RiskOutcome
from backend.decisions.repository import DecisionRepository
from backend.mcp_servers import notification_mcp_server
from backend.observability import TelemetryRepository, safe_error


class NotificationConflict(RuntimeError):
    """An idempotency key was reused for different notification content."""


class NotificationDeliveryError(RuntimeError):
    """The notification provider did not accept a delivery request."""


class NotificationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str
    event_type: Literal["decision_outcome", "run_summary"]
    run_id: str
    decision_id: str | None = None
    message: str = Field(min_length=1, max_length=1024)
    status: Literal["pending", "sending", "sent", "failed"]
    attempts: int = Field(ge=0)
    created_at: datetime
    last_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    error_summary: str | None = None


class DeliveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sent: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationRepository:
    def __init__(self, path: str | None = None):
        self.path = path or database.DB
        database.initialize_database(self.path)

    @staticmethod
    def _event(row: sqlite3.Row | None) -> NotificationEvent | None:
        return NotificationEvent.model_validate(dict(row)) if row else None

    def enqueue(
        self,
        *,
        event_key: str,
        event_type: Literal["decision_outcome", "run_summary"],
        run_id: str,
        message: str,
        decision_id: str | None = None,
    ) -> tuple[NotificationEvent, bool]:
        created_at = utc_now().isoformat()
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute(
                """INSERT INTO notification_outbox
                   (event_key, event_type, run_id, decision_id, message, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (event_key, event_type, run_id, decision_id, message, created_at),
            )
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE event_key=?", (event_key,)
            ).fetchone()
        event = self._event(row)
        if event is None:  # pragma: no cover - protected by the insert/select transaction
            raise KeyError(event_key)
        expected = (event_type, run_id, decision_id, message)
        stored = (event.event_type, event.run_id, event.decision_id, event.message)
        if stored != expected:
            raise NotificationConflict(
                f"notification event key {event_key!r} already has different content"
            )
        return event, cursor.rowcount == 1

    def ready(self, max_attempts: int, limit: int = 50) -> list[NotificationEvent]:
        stale_cutoff = (utc_now() - timedelta(minutes=10)).isoformat()
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """UPDATE notification_outbox SET status='failed',
                   error_summary='notification delivery was interrupted'
                   WHERE status='sending'
                     AND (last_attempt_at IS NULL OR last_attempt_at < ?)""",
                (stale_cutoff,),
            )
            rows = conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE status IN ('pending', 'failed') AND attempts < ?
                   ORDER BY created_at LIMIT ?""",
                (max_attempts, min(max(limit, 1), 200)),
            ).fetchall()
        return [NotificationEvent.model_validate(dict(row)) for row in rows]

    def start_attempt(self, event_key: str, max_attempts: int) -> NotificationEvent | None:
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE notification_outbox
                   SET status='sending', attempts=attempts+1, last_attempt_at=?,
                       error_summary=NULL
                   WHERE event_key=? AND status IN ('pending', 'failed') AND attempts < ?""",
                (utc_now().isoformat(), event_key, max_attempts),
            )
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE event_key=?", (event_key,)
            ).fetchone()
        return self._event(row) if cursor.rowcount == 1 else None

    def mark_sent(self, event_key: str) -> None:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """UPDATE notification_outbox SET status='sent', sent_at=?, error_summary=NULL
                   WHERE event_key=? AND status='sending'""",
                (utc_now().isoformat(), event_key),
            )
        if cursor.rowcount != 1:
            raise NotificationConflict(f"notification event {event_key!r} is not sending")

    def mark_failed(self, event_key: str, error: object) -> None:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """UPDATE notification_outbox SET status='failed', error_summary=?
                   WHERE event_key=? AND status='sending'""",
                (safe_error(error), event_key),
            )
        if cursor.rowcount != 1:
            raise NotificationConflict(f"notification event {event_key!r} is not sending")

    def for_run(self, run_id: str) -> list[NotificationEvent]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE run_id=? ORDER BY created_at, event_key""",
                (run_id,),
            ).fetchall()
        return [NotificationEvent.model_validate(dict(row)) for row in rows]


class NotificationSender(Protocol):
    async def send(self, message: str) -> None: ...


class NotificationSenderContext(Protocol):
    async def __aenter__(self) -> NotificationSender: ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...


class MCPNotificationSender:
    """Use one supervised notification MCP subprocess for a delivery batch."""

    def __init__(self, telemetry_repository: TelemetryRepository):
        self.telemetry_repository = telemetry_repository
        self.server = None

    async def __aenter__(self) -> "MCPNotificationSender":
        self.server = notification_mcp_server(self.telemetry_repository)
        await self.server.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.server is not None:
            await self.server.__aexit__(exc_type, exc, traceback)

    async def send(self, message: str) -> None:
        if self.server is None:  # pragma: no cover - guarded by the context manager
            raise RuntimeError("notification MCP server is not connected")
        result = await self.server.call_tool("push", {"args": {"message": message}})
        if getattr(result, "isError", False):
            raise NotificationDeliveryError("notification MCP tool returned an error")


SenderFactory = Callable[[], NotificationSenderContext]


class NotificationDispatcher:
    def __init__(
        self,
        path: str | None = None,
        *,
        sender_factory: SenderFactory | None = None,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        self.repository = NotificationRepository(path)
        telemetry = TelemetryRepository(self.repository.path)
        self.sender_factory = sender_factory or (lambda: MCPNotificationSender(telemetry))
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

    async def dispatch_pending(self) -> DeliveryReport:
        ready = self.repository.ready(self.max_attempts)
        if not ready:
            return DeliveryReport()

        sent = 0
        failed = 0
        try:
            async with self.sender_factory() as sender:
                for pending in ready:
                    while True:
                        event = self.repository.start_attempt(pending.event_key, self.max_attempts)
                        if event is None:
                            break
                        try:
                            await sender.send(event.message)
                        except Exception as exc:
                            self.repository.mark_failed(event.event_key, exc)
                            if event.attempts >= self.max_attempts:
                                failed += 1
                                break
                            await asyncio.sleep(
                                self.retry_backoff_seconds * (2 ** (event.attempts - 1))
                            )
                        else:
                            self.repository.mark_sent(event.event_key)
                            sent += 1
                            break
        except Exception as exc:
            for pending in ready:
                event = self.repository.start_attempt(pending.event_key, self.max_attempts)
                if event is not None:
                    self.repository.mark_failed(event.event_key, exc)
                    failed += 1
        return DeliveryReport(sent=sent, failed=failed)


def _decision_message(account_name: str, decision_id: str, path: str) -> str:
    decisions = DecisionRepository(path)
    decision = decisions.load_risk_decision(decision_id)
    proposal = decisions.load_proposal(str(decision.proposal_id))
    action = f"{proposal.side.value.upper()} {proposal.quantity} {proposal.symbol}"
    if decision.outcome == RiskOutcome.APPROVED:
        approved = decision.approved_quantity or proposal.quantity
        if approved != proposal.quantity:
            action = (
                f"{proposal.side.value.upper()} {approved} {proposal.symbol} "
                f"(requested {proposal.quantity})"
            )
        result = "approved by policy and paper-executed"
    elif decision.outcome == RiskOutcome.REJECTED:
        result = "rejected by deterministic policy; no paper order executed"
    else:
        result = "awaiting human approval; no paper order executed"
    return f"{account_name.title()} paper decision: {action} {result}."


def enqueue_run_notifications(
    record: AgentRunRecord, path: str | None = None
) -> list[NotificationEvent]:
    db_path = path or database.DB
    outbox = NotificationRepository(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cycles = conn.execute(
            """SELECT account_name, status, decision_ids FROM cycle_metrics
               WHERE run_id=? ORDER BY started_at""",
            (record.run_id,),
        ).fetchall()

    decision_ids: list[tuple[str, str]] = []
    for cycle in cycles:
        decision_ids.extend(
            (cycle["account_name"], decision_id)
            for decision_id in json.loads(cycle["decision_ids"])
        )
    for account_name, decision_id in decision_ids:
        outbox.enqueue(
            event_key=f"decision:{decision_id}:outcome",
            event_type="decision_outcome",
            run_id=record.run_id,
            decision_id=decision_id,
            message=_decision_message(account_name, decision_id, db_path),
        )

    succeeded = [row["account_name"].title() for row in cycles if row["status"] == "succeeded"]
    unsuccessful = [row["account_name"].title() for row in cycles if row["status"] != "succeeded"]
    if cycles:
        summary = (
            f"Paper run {record.run_id[:8]} {record.status.replace('_', ' ')}: "
            f"{len(succeeded)} succeeded, {len(unsuccessful)} unsuccessful"
        )
        if unsuccessful:
            summary += f" ({', '.join(unsuccessful)})"
        summary += f"; {len(decision_ids)} policy decisions."
    else:
        summary = (
            f"Paper run {record.run_id[:8]} {record.status.replace('_', ' ')}: "
            "no agent cycles completed."
        )
    outbox.enqueue(
        event_key=f"run:{record.run_id}:summary",
        event_type="run_summary",
        run_id=record.run_id,
        message=summary,
    )
    return outbox.for_run(record.run_id)


async def publish_run_notifications(
    record: AgentRunRecord,
    path: str | None = None,
    *,
    dispatcher: NotificationDispatcher | None = None,
) -> DeliveryReport:
    enqueue_run_notifications(record, path)
    active_dispatcher = dispatcher or NotificationDispatcher(path)
    return await active_dispatcher.dispatch_pending()
