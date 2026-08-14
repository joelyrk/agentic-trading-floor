"""Durable coordination for scheduled and manually requested agent runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

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
    retry_of: str | None = None


class AgentActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["pending", "running", "succeeded", "failed", "interrupted"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    requests: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    error_summary: str | None = None
    current_activity: str
    logs: list[dict[str, str]]


class AgentRunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: AgentRunRecord
    agents: list[AgentActivity]
    can_retry: bool = False
    retry_block_reason: str | None = None


AGENT_NAMES = ("warren", "george", "ray", "cathie")


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

    def progress(self, run_id: str, log_limit: int = 12) -> AgentRunProgress | None:
        """Return run-correlated stage logs and telemetry for the fixed four-agent roster."""
        run = self.get(run_id)
        if run is None:
            return None
        bounded_limit = min(max(log_limit, 1), 50)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cycles = {
                row["account_name"]: row
                for row in conn.execute(
                    "SELECT * FROM cycle_metrics WHERE run_id=? ORDER BY started_at", (run_id,)
                ).fetchall()
            }
            activities: list[AgentActivity] = []
            for name in AGENT_NAMES:
                cycle = cycles.get(name)
                rows = conn.execute(
                    """SELECT datetime, type, message FROM logs
                       WHERE name=? AND message LIKE ? ORDER BY id DESC LIMIT ?""",
                    (name, f"%Run {run_id}:%", bounded_limit),
                ).fetchall()
                logs = [dict(row) for row in reversed(rows)]
                if cycle is None:
                    status = "pending"
                    activity = (
                        "Waiting for an earlier agent to finish"
                        if run.status in {"queued", "running"}
                        else "Not started"
                    )
                    activities.append(
                        AgentActivity(
                            name=name, status=status, current_activity=activity, logs=logs
                        )
                    )
                    continue
                status = cycle["status"]
                activity = logs[-1]["message"].split(": ", 1)[-1] if logs else status
                activities.append(
                    AgentActivity(
                        name=name,
                        status=status,
                        started_at=cycle["started_at"],
                        completed_at=cycle["completed_at"],
                        requests=cycle["requests"],
                        total_tokens=cycle["total_tokens"],
                        latency_ms=cycle["latency_ms"],
                        error_summary=cycle["error_summary"],
                        current_activity=activity,
                        logs=logs,
                    )
                )
        can_retry, retry_block_reason = self.retryability(run_id)
        return AgentRunProgress(
            run=run,
            agents=activities,
            can_retry=can_retry,
            retry_block_reason=retry_block_reason,
        )

    def retryability(self, run_id: str) -> tuple[bool, str | None]:
        run = self.get(run_id)
        if run is None:
            return False, "run does not exist"
        if run.trigger != "manual":
            return False, "scheduled runs cannot be retried from the dashboard"
        if run.status not in {"failed", "interrupted"}:
            return False, "only failed or interrupted runs can be retried"
        completed = run.completed_at or utc_now()
        with sqlite3.connect(self.path) as conn:
            succeeded = conn.execute(
                "SELECT 1 FROM cycle_metrics WHERE run_id=? AND status='succeeded' LIMIT 1",
                (run_id,),
            ).fetchone()
            proposals = conn.execute(
                """SELECT 1 FROM trade_proposals
                   WHERE created_at >= ? AND created_at <= ? LIMIT 1""",
                (run.requested_at.isoformat(), completed.isoformat()),
            ).fetchone()
        if succeeded or proposals:
            return False, "the attempt may already have produced paper decisions"
        return True, None

    def retry(self, run_id: str, idempotency_key: str) -> AgentRunRecord:
        """Create a new audited attempt for a safe, proposal-free failed manual run."""
        can_retry, reason = self.retryability(run_id)
        if not can_retry:
            raise AgentRunConflict(reason or "run cannot be retried")
        prior = self.get(run_id)
        if prior is None:  # pragma: no cover - checked above
            raise KeyError(run_id)
        now = utc_now()
        new_run_id = str(uuid4())
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
                return record
            active = conn.execute(
                "SELECT run_id FROM agent_runs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active:
                raise AgentRunConflict(f"agent run {active['run_id']} is already active")
            conn.execute(
                """INSERT INTO agent_runs
                   (run_id, trigger, status, requested_at, requested_by, idempotency_key,
                    market_symbol, market_timestamp, market_retrieved_at, market_mode, retry_of)
                   VALUES (?, 'manual', 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_run_id,
                    now.isoformat(),
                    prior.requested_by,
                    idempotency_key,
                    prior.market_symbol,
                    prior.market_timestamp.isoformat(),
                    prior.market_retrieved_at.isoformat(),
                    prior.market_mode,
                    prior.run_id,
                ),
            )
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (new_run_id,)).fetchone()
            conn.commit()
        record = self._record(row)
        if record is None:  # pragma: no cover
            raise KeyError(new_run_id)
        return record

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
