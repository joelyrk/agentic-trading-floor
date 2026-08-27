"""Durable, credential-safe runtime health, cycle metrics, and budget controls."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import uuid4

from agents import RunHooks, Usage
from pydantic import BaseModel, ConfigDict, Field

from backend import database


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+"),
    re.compile(r"(?i)(api[_-]?key|token|authorization|secret|password)\s*[=:]\s*[^\s,;]+"),
    re.compile(r"\b(sk|tvly|pplx|xai)-[A-Za-z0-9_-]{8,}\b"),
)


def safe_error(error: object, limit: int = 500) -> str:
    """Return an attributable diagnostic without credentials or sprawling payloads."""
    text = " ".join(str(error).split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else 'secret'}=[REDACTED]",
            text,
        )
    return (text or type(error).__name__)[:limit]


class ServiceState(str):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ServiceHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    state: str
    required: bool
    last_success: datetime | None = None
    last_error: datetime | None = None
    error_summary: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    circuit_open_until: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    active: bool = True


class CycleBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_turns: int = Field(default=8, gt=0)
    max_tokens: int = Field(default=40_000, gt=0)
    max_wall_seconds: float = Field(default=180, gt=0)
    max_spend_usd: Decimal = Field(default=Decimal("5"), gt=0)
    input_cost_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    output_cost_per_million: Decimal = Field(default=Decimal("0"), ge=0)

    @classmethod
    def from_env(cls) -> "CycleBudget":
        return cls(
            max_turns=os.getenv("CYCLE_MAX_TURNS", "8"),
            max_tokens=os.getenv("CYCLE_MAX_TOKENS", "40000"),
            max_wall_seconds=os.getenv("CYCLE_MAX_WALL_SECONDS", "180"),
            max_spend_usd=os.getenv("CYCLE_MAX_SPEND_USD", "5"),
            input_cost_per_million=os.getenv("MODEL_INPUT_COST_PER_MILLION", "0"),
            output_cost_per_million=os.getenv("MODEL_OUTPUT_COST_PER_MILLION", "0"),
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * self.input_cost_per_million
            + Decimal(output_tokens) * self.output_cost_per_million
        ) / Decimal(1_000_000)


class BudgetExceeded(RuntimeError):
    pass


class BudgetHooks(RunHooks):
    """Stop between model requests once token or spend budgets are exhausted."""

    def __init__(self, budget: CycleBudget):
        self.budget = budget
        self.usage = None

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        usage = context.usage
        self.usage = usage
        cost = self.budget.estimate_cost(usage.input_tokens, usage.output_tokens)
        if usage.total_tokens >= self.budget.max_tokens:
            raise BudgetExceeded(
                f"cycle token budget exceeded ({usage.total_tokens}/{self.budget.max_tokens})"
            )
        if cost >= self.budget.max_spend_usd:
            raise BudgetExceeded(
                f"cycle spend budget exceeded ({cost}/{self.budget.max_spend_usd} USD)"
            )

    async def on_llm_end(self, context, agent, response) -> None:
        self.usage = context.usage

    async def capture_run_error(self, handler_input) -> None:
        """Capture completed model usage before a terminal runner error is re-raised."""
        response_usage = Usage()
        for response in handler_input.run_data.raw_responses:
            response_usage.add(response.usage)
        context_usage = handler_input.context.usage
        self.usage = (
            response_usage
            if response_usage.total_tokens > context_usage.total_tokens
            else context_usage
        )
        return None


@dataclass(frozen=True)
class CycleContext:
    cycle_id: str
    run_id: str | None
    scenario_id: str | None
    started_at: datetime

    @classmethod
    def create(cls, *, run_id: str | None = None, scenario_id: str | None = None) -> "CycleContext":
        return cls(str(uuid4()), run_id, scenario_id, utc_now())


class TelemetryRepository:
    def __init__(self, path: str | None = None):
        self.path = path or database.DB
        database.initialize_database(self.path)

    def register_service(self, name: str, required: bool) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO service_health
                   (name, state, required, consecutive_failures, active)
                   VALUES (?, 'unavailable', ?, 0, 1)
                   ON CONFLICT(name) DO UPDATE SET required=excluded.required, active=1""",
                (name, int(required)),
            )

    def retire_services_except(self, names: tuple[str, ...]) -> None:
        """Hide retired integrations while preserving their historical health records."""
        placeholders = ",".join("?" for _ in names)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                f"UPDATE service_health SET active=0 WHERE name NOT IN ({placeholders})",
                names,
            )

    def service(self, name: str) -> ServiceHealth | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM service_health WHERE name = ?", (name,)).fetchone()
        return ServiceHealth.model_validate(dict(row)) if row else None

    def services(self) -> list[ServiceHealth]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM service_health WHERE active=1 ORDER BY name"
            ).fetchall()
        return [ServiceHealth.model_validate(dict(row)) for row in rows]

    def mark_starting(self, name: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE service_health SET state='starting' WHERE name=?", (name,))

    def mark_success(self, name: str, latency_ms: float) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE service_health SET state='healthy', last_success=?, latency_ms=?,
                   consecutive_failures=0, error_summary=NULL, circuit_open_until=NULL,
                   attempt_count=attempt_count+1 WHERE name=?""",
                (utc_now().isoformat(), latency_ms, name),
            )

    def mark_failure(
        self, name: str, error: object, *, threshold: int, reset_seconds: float
    ) -> None:
        current = self.service(name)
        failures = (current.consecutive_failures if current else 0) + 1
        opened = utc_now() + timedelta(seconds=reset_seconds) if failures >= threshold else None
        state = ServiceState.UNAVAILABLE if opened else ServiceState.DEGRADED
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE service_health SET state=?, last_error=?, error_summary=?,
                   consecutive_failures=?, circuit_open_until=?, attempt_count=attempt_count+1,
                   failure_count=failure_count+1 WHERE name=?""",
                (
                    state,
                    utc_now().isoformat(),
                    safe_error(error),
                    failures,
                    opened.isoformat() if opened else None,
                    name,
                ),
            )

    def circuit_is_open(self, name: str) -> bool:
        health = self.service(name)
        return bool(health and health.circuit_open_until and health.circuit_open_until > utc_now())

    def start_cycle(
        self,
        context: CycleContext,
        account_name: str,
        model: str,
        prompt_version: str,
        market_mode: str,
        budget: CycleBudget,
    ) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO cycle_metrics
                   (cycle_id, account_name, run_id, scenario_id, model, prompt_version, market_mode,
                    started_at, status, budget)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (
                    context.cycle_id,
                    account_name.lower(),
                    context.run_id,
                    context.scenario_id,
                    model,
                    prompt_version,
                    market_mode,
                    context.started_at.isoformat(),
                    budget.model_dump_json(),
                ),
            )

    def recover_interrupted_cycles(self, reason: str = "process restarted during cycle") -> int:
        """Close orphaned running cycles before a scheduler begins new work."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """UPDATE cycle_metrics SET completed_at=?, status='interrupted', error_summary=?
                   WHERE status='running'""",
                (utc_now().isoformat(), safe_error(reason)),
            )
        return cursor.rowcount

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: str,
        usage: Any | None,
        latency_ms: float,
        estimated_cost: Decimal,
        error: object | None = None,
        decision_ids: list[str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        requests = int(getattr(usage, "requests", 0)) if usage else 0
        input_tokens = int(getattr(usage, "input_tokens", 0)) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0)) if usage else 0
        total_tokens = int(getattr(usage, "total_tokens", 0)) if usage else 0
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE cycle_metrics SET completed_at=?, status=?, requests=?, input_tokens=?,
                   output_tokens=?, total_tokens=?, estimated_cost_usd=?, latency_ms=?, error_summary=?,
                   decision_ids=? WHERE cycle_id=?""",
                (
                    utc_now().isoformat(),
                    status,
                    requests,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    str(estimated_cost),
                    latency_ms,
                    safe_error(error) if error else None,
                    json.dumps(decision_ids or []),
                    cycle_id,
                ),
            )
            if trace_id:
                conn.executemany(
                    """INSERT OR REPLACE INTO decision_telemetry
                       (decision_id, cycle_id, trace_id, recorded_at) VALUES (?, ?, ?, ?)""",
                    [
                        (decision_id, cycle_id, trace_id, utc_now().isoformat())
                        for decision_id in decision_ids or []
                    ],
                )

    def decision_metadata(self, decision_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT dt.decision_id, dt.cycle_id, dt.trace_id, cm.run_id, cm.scenario_id,
                          cm.model, cm.prompt_version, cm.market_mode, cm.requests, cm.input_tokens,
                          cm.output_tokens, cm.total_tokens, cm.estimated_cost_usd, cm.latency_ms,
                          cm.status
                   FROM decision_telemetry dt JOIN cycle_metrics cm ON cm.cycle_id=dt.cycle_id
                   WHERE dt.decision_id=?""",
                (decision_id,),
            ).fetchone()
        return dict(row) if row else None

    def health_payload(self, market_status: dict[str, Any]) -> dict[str, Any]:
        services = [item.model_dump(mode="json") for item in self.services()]
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            current = conn.execute(
                "SELECT * FROM cycle_metrics ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            totals = conn.execute(
                """SELECT COUNT(*) cycles,
                          SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) successes,
                          COALESCE(SUM(requests),0) requests,
                          COALESCE(SUM(total_tokens),0) tokens,
                          COALESCE(SUM(CAST(estimated_cost_usd AS REAL)),0) cost,
                          COALESCE(AVG(latency_ms),0) latency
                   FROM cycle_metrics"""
            ).fetchone()
        cycles = totals["cycles"]
        service_failures = sum(item["failure_count"] for item in services)
        service_attempts = sum(item["attempt_count"] for item in services)
        return {
            "status": "degraded"
            if market_status.get("degraded") or any(s["state"] != "healthy" for s in services)
            else "healthy",
            "current_cycle_id": current["cycle_id"]
            if current and current["status"] == "running"
            else None,
            "latest_cycle": dict(current) if current else None,
            "services": services,
            "data_freshness": {
                "mode": market_status.get("mode"),
                "degraded": market_status.get("degraded"),
                "freshness_threshold_seconds": market_status.get("freshness_threshold_seconds"),
                "last_observation": market_status.get("last_successful_observation"),
            },
            "metrics": {
                "request_count": totals["requests"],
                "token_count": totals["tokens"],
                "estimated_cost_usd": totals["cost"],
                "average_cycle_latency_ms": totals["latency"],
                "mcp_failure_rate": service_failures / service_attempts if service_attempts else 0,
                "cycle_success_rate": totals["successes"] / cycles if cycles else 0,
            },
        }


async def measured(coro, timeout_seconds: float) -> tuple[Any, float]:
    started = monotonic()
    async with asyncio.timeout(timeout_seconds):
        result = await coro
    return result, (monotonic() - started) * 1000
