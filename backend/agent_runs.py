"""Durable coordination for scheduled and manually requested agent runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from backend import database
from backend.market.models import MarketObservation
from backend.observability import safe_error


class AgentRunConflict(RuntimeError):
    """Another coordinated agent run is queued or active."""


class UnchangedMarketData(RuntimeError):
    """The observed EOD snapshot has already been consumed by a run."""


class AgentRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    trigger: Literal["scheduled", "manual"]
    status: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    requested_by: str
    idempotency_key: str
    market_symbol: str
    market_timestamp: datetime
    market_retrieved_at: datetime
    market_mode: str
    error_summary: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRunRepository:
    def __init__(self, path: str | None = None):
        self.path = path or database.DB
        database.initialize_database(self.path)

    @staticmethod
    def _record(row: sqlite3.Row | None) -> AgentRunRecord | None:
        return AgentRunRecord.model_validate(dict(row)) if row else None

    def get(self, run_id: str) -> AgentRunRecord | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._record(row)

    def latest(self) -> AgentRunRecord | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_runs ORDER BY requested_at DESC LIMIT 1"
            ).fetchone()
        return self._record(row)

    def recover_stale(self, max_age: timedelta = timedelta(minutes=20)) -> int:
        cutoff = (utc_now() - max_age).isoformat()
        with sqlite3.connect(self.path, timeout=30) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs SET status='interrupted', completed_at=?,
                   error_summary='agent run exceeded its recovery window'
                   WHERE status IN ('queued', 'running') AND requested_at < ?""",
                (utc_now().isoformat(), cutoff),
            )
        return cursor.rowcount

    def request(
        self,
        *,
        trigger: Literal["scheduled", "manual"],
        requested_by: str,
        idempotency_key: str,
        observation: MarketObservation,
    ) -> tuple[AgentRunRecord, bool]:
        """Atomically reserve one market snapshot and reject overlapping runs."""
        self.recover_stale()
        now = utc_now()
        run_id = str(uuid4())
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM agent_runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                conn.commit()
                record = self._record(existing)
                if record is None:  # pragma: no cover
                    raise KeyError(idempotency_key)
                return record, False
            active = conn.execute(
                "SELECT run_id FROM agent_runs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active:
                raise AgentRunConflict(f"agent run {active['run_id']} is already active")
            consumed = conn.execute(
                """SELECT run_id FROM agent_runs
                   WHERE market_mode=? AND market_timestamp=? LIMIT 1""",
                (observation.mode.value, observation.market_timestamp.isoformat()),
            ).fetchone()
            if consumed:
                raise UnchangedMarketData(
                    "market data has not changed since run " + consumed["run_id"]
                )
            conn.execute(
                """INSERT INTO agent_runs
                   (run_id, trigger, status, requested_at, requested_by, idempotency_key,
                    market_symbol, market_timestamp, market_retrieved_at, market_mode)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    trigger,
                    now.isoformat(),
                    requested_by,
                    idempotency_key,
                    observation.symbol,
                    observation.market_timestamp.isoformat(),
                    observation.retrieved_at.isoformat(),
                    observation.mode.value,
                ),
            )
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            conn.commit()
        record = self._record(row)
        if record is None:  # pragma: no cover
            raise KeyError(run_id)
        return record, True

    def mark_running(self, run_id: str) -> AgentRunRecord:
        with sqlite3.connect(self.path, timeout=30) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs SET status='running', started_at=?
                   WHERE run_id=? AND status='queued'""",
                (utc_now().isoformat(), run_id),
            )
            if cursor.rowcount != 1:
                raise AgentRunConflict(f"agent run {run_id} is not queued")
        record = self.get(run_id)
        if record is None:  # pragma: no cover - protected by the update above
            raise KeyError(run_id)
        return record

    def finish(
        self, run_id: str, status: Literal["succeeded", "failed", "interrupted"], error=None
    ) -> AgentRunRecord:
        with sqlite3.connect(self.path, timeout=30) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs SET status=?, completed_at=?, error_summary=?
                   WHERE run_id=? AND status IN ('queued', 'running')""",
                (status, utc_now().isoformat(), safe_error(error) if error else None, run_id),
            )
            if cursor.rowcount != 1:
                raise AgentRunConflict(f"agent run {run_id} is not active")
        record = self.get(run_id)
        if record is None:  # pragma: no cover
            raise KeyError(run_id)
        return record

    def cycle_outcome(self, run_id: str, expected_cycles: int) -> tuple[str, str | None]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT status, error_summary FROM cycle_metrics WHERE run_id=?", (run_id,)
            ).fetchall()
        if len(rows) == expected_cycles and all(row["status"] == "succeeded" for row in rows):
            return "succeeded", None
        errors = [row["error_summary"] for row in rows if row["error_summary"]]
        summary = (
            "; ".join(errors) if errors else f"only {len(rows)}/{expected_cycles} cycles completed"
        )
        return "failed", summary
