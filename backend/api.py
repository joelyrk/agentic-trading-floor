"""HTTP API over the trading floor, for a separate frontend to consume.

The trading floor persists its state out of band and this serves it as JSON.
Account mutation is limited to explicit approval of policy-gated paper orders;
replay and experiment writes affect only offline evaluation records.

Run it from the 6_mcp directory so it shares the engine's accounts.db:

    uv run uvicorn backend.api:app --port 8000
"""

import asyncio
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

import backend.startup as startup
from backend import market
from backend.access import AccessControlMiddleware, ReadOnlyModeMiddleware
from backend.accounts import Account
from backend.agent_runs import AgentRunConflict, AgentRunRepository, UnchangedMarketData
from backend.config import validate_startup
from backend.database import read_latest_market_observation, read_log, write_market_observation
from backend.decisions import ExecutionService, RiskPolicy
from backend.decisions.repository import DecisionRepository, ExecutionConflict
from backend.observability import TelemetryRepository
from backend.product import ExperimentRequest, ProductService, ReplayRequest
from backend.strategies import ensure_default_strategies
from backend.trading_floor import (
    execute_agent_run,
    lastnames,
    names,
    reserve_agent_run,
    short_model_names,
)

# Mirrors the log colours in demo/ so the frontend reproduces the same panel.
LOG_COLORS = {
    "trace": "#87CEEB",
    "agent": "#00dddd",
    "function": "#00dd00",
    "generation": "#dddd00",
    "response": "#aa00dd",
    "account": "#dd0000",
}
DEFAULT_LOG_COLOR = "#87CEEB"

roster = [
    {"name": name, "lastname": lastname, "model_name": model_name}
    for name, lastname, model_name in zip(names, lastnames, short_model_names)
]
roster_by_name = {trader["name"].lower(): trader for trader in roster}

validate_startup("api")
app = FastAPI(
    title="Agentic Trading Floor API",
    version="1.0.0",
    description=(
        "Auditable paper-trading and point-in-time evaluation API. No endpoint places real trades."
    ),
)
app.add_middleware(AccessControlMiddleware, settings=startup.api_access_settings)
app.add_middleware(
    ReadOnlyModeMiddleware,
    read_only=startup.application_settings.read_only,
    detail=(
        "public showcase is read-only; scheduled AI runs remain enabled"
        if startup.application_settings.public_showcase
        else "seeded demo mode is read-only"
    ),
)
market_service = market.get_market_service()  # Fail startup on invalid capability config.
if startup.application_settings.mode == "demo":
    from backend.demo import seed_demo_database

    seed_demo_database(startup.runtime_settings.accounts_db)
    market_service.observe("SPY")
else:
    ensure_default_strategies()
decision_repository = DecisionRepository()
telemetry_repository = TelemetryRepository()
product_service = ProductService()
agent_run_repository = AgentRunRepository()
manual_run_tasks: dict[str, asyncio.Task] = {}


class ManualAgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    confirm_paper_trading: Literal[True]


def _track_manual_run(run_id: str, task: asyncio.Task) -> None:
    manual_run_tasks[run_id] = task

    def finished(completed: asyncio.Task) -> None:
        manual_run_tasks.pop(run_id, None)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)


def average_cost(account: Account, symbol: str) -> float:
    """Average price paid across this symbol's buys, for per-holding profit."""
    spend = sum(
        t.price * t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0
    )
    bought = sum(t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0)
    return spend / bought if bought else 0.0


def holdings_detail(account: Account) -> tuple[list[dict], dict]:
    """Current holdings enriched with price, market value and unrealised profit."""
    details = []
    degraded_symbols: list[str] = []
    errors: list[str] = []
    valuation_id = str(uuid4())
    for symbol, quantity in account.holdings.items():
        try:
            observation = market.get_market_observation(symbol)
            observation_id = (
                f"demo-valuation:{account.name}:{symbol}"
                if startup.application_settings.read_only
                else write_market_observation(account.name, "valuation", valuation_id, observation)
            )
        except market.MarketDataError as exc:
            persisted = read_latest_market_observation(account.name, symbol)
            if persisted is None:
                raise
            observation = market.MarketObservation.model_validate(persisted["observation"])
            observation_id = persisted["id"]
            degraded_symbols.append(symbol)
            errors.append(f"{symbol}: {exc}")
        price = float(observation.price)
        cost = average_cost(account, symbol)
        details.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "avg_cost": cost,
                "market_value": price * quantity,
                "unrealized_pnl": (price - cost) * quantity,
                "market_observation_id": observation_id,
                "market_observation": observation.model_dump(mode="json"),
            }
        )
    return details, {
        "state": "degraded" if degraded_symbols else "healthy",
        "used_persisted_observations": degraded_symbols,
        "error_summary": "; ".join(errors) or None,
    }


def require_trader(name: str) -> dict:
    trader = roster_by_name.get(name.lower())
    if not trader:
        raise HTTPException(status_code=404, detail=f"Unknown trader {name}")
    return trader


@app.get("/api/traders")
def get_traders() -> list[dict]:
    """The four traders on the floor."""
    return roster


@app.get("/api/runtime")
def get_runtime() -> dict:
    """Expose deployment semantics without returning configuration or secrets."""
    return {
        "mode": startup.application_settings.mode,
        "read_only": startup.application_settings.read_only,
        "public_showcase": startup.application_settings.public_showcase,
        "scheduled_ai_enabled": startup.application_settings.scheduled_ai_enabled,
        "paper_trading_only": True,
        "credentials_required": False if startup.application_settings.mode == "demo" else None,
    }


@app.get("/api/market")
def get_market() -> dict:
    """Report effective/configured price mode and current data health."""
    return market_service.status().model_dump(mode="json")


@app.get("/api/health")
def get_health() -> dict:
    """One credential-safe view of service, cycle, freshness, latency, and cost health."""
    market_status = market_service.status().model_dump(mode="json")
    return telemetry_repository.health_payload(market_status)


@app.get("/api/agent-runs/latest")
def get_latest_agent_run() -> dict | None:
    """Return the most recent scheduled or manual run request and its audit state."""
    record = agent_run_repository.latest()
    return record.model_dump(mode="json") if record else None


@app.get("/api/agent-runs/{run_id}")
def get_agent_run(run_id: UUID) -> dict:
    """Return one immutable-identity run while its lifecycle fields advance."""
    record = agent_run_repository.get(str(run_id))
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown agent run {run_id}")
    return record.model_dump(mode="json")


@app.get("/api/agent-runs/{run_id}/progress")
def get_agent_run_progress(run_id: UUID, log_limit: int = 12) -> dict:
    """Show each agent's current stage and safe run-correlated log tail."""
    progress = agent_run_repository.progress(str(run_id), log_limit=log_limit)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"unknown agent run {run_id}")
    return progress.model_dump(mode="json")


@app.post("/api/agent-runs", status_code=202)
async def create_manual_agent_run(request: ManualAgentRunRequest) -> dict:
    """Reserve fresh market data and start one bounded, sequential paper cycle."""
    try:
        runtime = validate_startup("scheduler")
        record, created = await reserve_agent_run(
            agent_run_repository,
            trigger="manual",
            requested_by=(
                "local-console"
                if startup.api_access_settings.access_mode == "local"
                else "authenticated-api"
            ),
            idempotency_key=str(request.idempotency_key),
        )
    except AgentRunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnchangedMarketData as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except market.MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if created:
        task = asyncio.create_task(
            execute_agent_run(
                record.run_id,
                repository=agent_run_repository,
                max_concurrency=runtime.agent_max_concurrency,
            )
        )
        _track_manual_run(record.run_id, task)
    return record.model_dump(mode="json")


@app.post("/api/agent-runs/{run_id}/cancel")
async def cancel_manual_agent_run(run_id: UUID) -> dict:
    """Cancel one active manual task and let cycle/run handlers persist interruption."""
    record = agent_run_repository.get(str(run_id))
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown agent run {run_id}")
    if record.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="agent run is already complete")
    task = manual_run_tasks.get(str(run_id))
    if task is None:
        raise HTTPException(status_code=409, detail="run is not cancellable from this API process")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    updated = agent_run_repository.get(str(run_id))
    if updated is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail=f"unknown agent run {run_id}")
    if updated.status in {"queued", "running"}:
        try:
            updated = agent_run_repository.finish(
                str(run_id), "interrupted", "cancelled from dashboard"
            )
        except AgentRunConflict:
            updated = agent_run_repository.get(str(run_id)) or updated
    return updated.model_dump(mode="json")


@app.post("/api/agent-runs/{run_id}/retry", status_code=202)
async def retry_manual_agent_run(run_id: UUID, request: ManualAgentRunRequest) -> dict:
    """Retry a proposal-free failed attempt against its already-audited EOD snapshot."""
    try:
        runtime = validate_startup("scheduler")
        record = agent_run_repository.retry(str(run_id), str(request.idempotency_key))
    except AgentRunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record.status == "queued" and record.run_id not in manual_run_tasks:
        task = asyncio.create_task(
            execute_agent_run(
                record.run_id,
                repository=agent_run_repository,
                max_concurrency=runtime.agent_max_concurrency,
            )
        )
        _track_manual_run(record.run_id, task)
    return record.model_dump(mode="json")


@app.get("/api/risk")
def get_risk_policy() -> dict:
    """Return deterministic policy limits used to calculate UI utilization."""
    return RiskPolicy.from_env().model_dump(mode="json")


@app.get("/api/traders/{name}")
def get_trader(name: str) -> dict:
    """A trader's full state: value, profit, holdings, transactions and history."""
    trader = require_trader(name)
    account = Account.get(name)
    try:
        holdings, valuation_status = holdings_detail(account)
    except market.MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    portfolio_value = account.balance + sum(h["market_value"] for h in holdings)
    return {
        "name": trader["name"],
        "lastname": trader["lastname"],
        "model_name": trader["model_name"],
        "balance": account.balance,
        "strategy": account.strategy,
        "portfolio_value": portfolio_value,
        "pnl": account.calculate_profit_loss(portfolio_value),
        "holdings": holdings,
        "valuation_status": valuation_status,
        "transactions": account.list_transactions(),
        "time_series": [
            {"datetime": ts, "value": value} for ts, value in account.portfolio_value_time_series
        ],
    }


@app.get("/api/traders/{name}/logs")
def get_trader_logs(name: str, last_n: int = 13) -> list[dict]:
    """Recent trace and account log lines, oldest first, with their panel colour."""
    require_trader(name)
    rows = list(read_log(name, last_n))
    return [
        {
            "datetime": ts,
            "type": kind,
            "message": message,
            "color": LOG_COLORS.get(kind, DEFAULT_LOG_COLOR),
        }
        for ts, kind, message in rows
    ]


@app.get("/api/traders/{name}/decisions")
def get_trader_decisions(name: str) -> list[dict]:
    """Auditable proposal → risk → paper-order → execution chains."""
    require_trader(name)
    return decision_repository.audit_chain(name)


@app.get("/api/evidence/{proposal_id}")
def get_decision_evidence(proposal_id: str) -> dict:
    """Citation-linked evidence, observation, policy result, and paper execution."""
    try:
        evidence = decision_repository.evidence_chain(proposal_id)
        decision = evidence.get("risk_decision")
        evidence["telemetry"] = (
            telemetry_repository.decision_metadata(decision["decision_id"]) if decision else None
        )
        return evidence
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/decisions/{decision_id}/approve")
def approve_high_risk_decision(decision_id: str) -> dict:
    """Explicitly approve and execute a pending high-risk paper proposal."""
    policy = RiskPolicy.from_env()
    if not policy.human_approval_enabled or policy.automated_replay:
        raise HTTPException(status_code=409, detail="human approval policy is not active")
    try:
        decision = decision_repository.approve_human(decision_id, datetime.now(timezone.utc))
        proposal = decision_repository.load_proposal(str(decision.proposal_id))
        execution = ExecutionService(decision_repository).execute(proposal, decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "risk_decision": decision.model_dump(mode="json"),
        "execution": execution.model_dump(mode="json") if execution else None,
    }


@app.get("/api/replay/scenarios")
def get_replay_scenarios() -> dict:
    """List point-in-time inputs; outcome fields are deliberately absent."""
    return product_service.scenarios()


@app.post("/api/replays")
def create_replay(request: ReplayRequest) -> dict:
    """Execute and persist one offline decision without revealing its outcome."""
    try:
        return product_service.create_replay(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/replays/{replay_id}")
def get_replay(replay_id: str) -> dict:
    try:
        return product_service.replay(replay_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/replays/{replay_id}/reveal")
def reveal_replay_outcome(replay_id: str) -> dict:
    """Reveal fixture outcomes only after a persisted decision is complete."""
    try:
        return product_service.reveal(replay_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/experiments")
def get_experiments() -> list[dict]:
    """Return versioned offline evaluation reports for comparison."""
    return product_service.experiments()


@app.post("/api/experiments")
def create_experiment(request: ExperimentRequest) -> dict:
    """Run a credential-free evaluation across baselines and architectures."""
    return product_service.run_experiment(request)
