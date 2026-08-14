"""HTTP API over the trading floor, for a separate frontend to consume.

The trading floor persists its state out of band and this serves it as JSON.
Account mutation is limited to explicit approval of policy-gated paper orders.

Run it from the 6_mcp directory so it shares the engine's accounts.db:

    uv run uvicorn backend.api:app --port 8000
"""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException

from backend import market
from backend.accounts import Account
from backend.database import read_log, write_market_observation
from backend.trading_floor import names, lastnames, short_model_names
from backend.decisions import ExecutionService, RiskPolicy
from backend.decisions.repository import DecisionRepository, ExecutionConflict
from backend.observability import TelemetryRepository

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

app = FastAPI(title="Trading Floor")
market_service = market.get_market_service()  # Fail startup on invalid capability config.
decision_repository = DecisionRepository()
telemetry_repository = TelemetryRepository()


def average_cost(account: Account, symbol: str) -> float:
    """Average price paid across this symbol's buys, for per-holding profit."""
    spend = sum(t.price * t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0)
    bought = sum(t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0)
    return spend / bought if bought else 0.0


def holdings_detail(account: Account) -> list[dict]:
    """Current holdings enriched with price, market value and unrealised profit."""
    details = []
    valuation_id = str(uuid4())
    for symbol, quantity in account.holdings.items():
        observation = market.get_market_observation(symbol)
        observation_id = write_market_observation(
            account.name, "valuation", valuation_id, observation
        )
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
    return details


def require_trader(name: str) -> dict:
    trader = roster_by_name.get(name.lower())
    if not trader:
        raise HTTPException(status_code=404, detail=f"Unknown trader {name}")
    return trader


@app.get("/api/traders")
def get_traders() -> list[dict]:
    """The four traders on the floor."""
    return roster


@app.get("/api/market")
def get_market() -> dict:
    """Report effective/configured price mode and current data health."""
    return market_service.status().model_dump(mode="json")


@app.get("/api/health")
def get_health() -> dict:
    """One credential-safe view of service, cycle, freshness, latency, and cost health."""
    market_status = market_service.status().model_dump(mode="json")
    return telemetry_repository.health_payload(market_status)


@app.get("/api/traders/{name}")
def get_trader(name: str) -> dict:
    """A trader's full state: value, profit, holdings, transactions and history."""
    trader = require_trader(name)
    account = Account.get(name)
    try:
        holdings = holdings_detail(account)
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
        "transactions": account.list_transactions(),
        "time_series": [{"datetime": ts, "value": value} for ts, value in account.portfolio_value_time_series],
    }


@app.get("/api/traders/{name}/logs")
def get_trader_logs(name: str, last_n: int = 13) -> list[dict]:
    """Recent trace and account log lines, oldest first, with their panel colour."""
    require_trader(name)
    rows = list(read_log(name, last_n))
    return [
        {"datetime": ts, "type": kind, "message": message, "color": LOG_COLORS.get(kind, DEFAULT_LOG_COLOR)}
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
            telemetry_repository.decision_metadata(decision["decision_id"])
            if decision else None
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
        decision = decision_repository.approve_human(
            decision_id, datetime.now(timezone.utc)
        )
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
