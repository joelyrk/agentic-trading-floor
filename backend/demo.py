"""Deterministic, auditable seed data for the credential-free read-only demo."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.accounts import INITIAL_BALANCE, SPREAD, Transaction
from backend.decisions.models import (
    ExecutionResult,
    ExecutionStatus,
    OrderSide,
    PaperOrder,
    RiskDecision,
    RiskOutcome,
    RiskRuleResult,
    TradeProposal,
)
from backend.market.models import DataMode, MarketObservation, ObservationSource
from backend.migrations import migrate
from backend.observability import CycleBudget
from backend.research.models import EvidenceClaim, EvidenceStance, ResearchBrief, SourceRecord

DEMO_SEED_VERSION = "phase8-v1"
DEMO_AS_OF = datetime(2026, 8, 1, 20, 5, tzinfo=timezone.utc)


def _id(kind: str, name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"agentic-trading-floor:demo:{DEMO_SEED_VERSION}:{kind}:{name}")


def _observation(symbol: str, price: str, offset: int) -> MarketObservation:
    retrieved_at = DEMO_AS_OF + timedelta(minutes=offset)
    return MarketObservation(
        symbol=symbol,
        price=Decimal(price),
        currency="USD",
        market_timestamp=retrieved_at - timedelta(minutes=5),
        retrieved_at=retrieved_at,
        source=ObservationSource.SIMULATOR,
        mode=DataMode.SIMULATED,
        is_stale=False,
        provider_endpoint="deterministic-demo-fixture/v1",
    )


def _account_payload(
    name: str,
    strategy: str,
    proposal: TradeProposal,
    approved: bool,
) -> dict:
    if not approved:
        return {
            "name": name,
            "balance": INITIAL_BALANCE,
            "strategy": strategy,
            "holdings": {},
            "transactions": [],
            "portfolio_value_time_series": [
                ((DEMO_AS_OF - timedelta(days=2)).isoformat(), INITIAL_BALANCE),
                ((DEMO_AS_OF - timedelta(days=1)).isoformat(), INITIAL_BALANCE),
                (DEMO_AS_OF.isoformat(), INITIAL_BALANCE),
            ],
        }
    execution_price = proposal.market_observation.price * (Decimal("1") + Decimal(str(SPREAD)))
    balance = Decimal(str(INITIAL_BALANCE)) - execution_price * proposal.quantity
    transaction = Transaction(
        symbol=proposal.symbol,
        quantity=proposal.quantity,
        price=float(execution_price),
        timestamp=(proposal.created_at + timedelta(seconds=2)).isoformat(),
        rationale=proposal.rationale,
        market_observation_id=f"demo-order:{_id('order', name)}",
        market_observation=proposal.market_observation,
    )
    value = float(balance + proposal.market_observation.price * proposal.quantity)
    return {
        "name": name,
        "balance": float(balance),
        "strategy": strategy,
        "holdings": {proposal.symbol: proposal.quantity},
        "transactions": [transaction.model_dump(mode="json")],
        "portfolio_value_time_series": [
            ((DEMO_AS_OF - timedelta(days=2)).isoformat(), INITIAL_BALANCE),
            ((DEMO_AS_OF - timedelta(days=1)).isoformat(), INITIAL_BALANCE - 8),
            (DEMO_AS_OF.isoformat(), value),
        ],
    }


def seed_demo_database(path: str | Path) -> bool:
    """Seed once in a single transaction; return True only when data was inserted."""
    db_path = str(path)
    migrate(db_path)
    entries = (
        ("warren", "AAPL", "212.40", 8, True, "Quality compounders with measured sizing"),
        ("george", "NVDA", "174.60", 20, False, "Concentrated growth with hard notional caps"),
        ("ray", "MSFT", "421.15", 6, True, "Diversified systems with deterministic limits"),
        ("cathie", "COIN", "318.25", 12, False, "Innovation themes constrained by risk policy"),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS demo_seed_state "
            "(version TEXT PRIMARY KEY, seeded_at TEXT NOT NULL)"
        )
        if conn.execute(
            "SELECT 1 FROM demo_seed_state WHERE version=?", (DEMO_SEED_VERSION,)
        ).fetchone():
            return False
        for offset, (name, symbol, price, quantity, approved, strategy) in enumerate(entries):
            observation = _observation(symbol, price, offset)
            created_at = observation.retrieved_at + timedelta(minutes=1)
            source = SourceRecord(
                source_id=f"demo-source-{name}",
                canonical_url=(
                    f"https://www.sec.gov/Archives/edgar/data/{1000 + offset}/"
                    f"{symbol.lower()}-demo.html"
                ),
                publisher="U.S. Securities and Exchange Commission",
                title=f"Demonstration filing context for {symbol}",
                published_at=created_at - timedelta(days=2),
                retrieved_at=created_at - timedelta(minutes=1),
                supporting_excerpt=(
                    f"Synthetic demo excerpt for {symbol}; included only to exercise citation audit."
                ),
                caveats=["Seeded demonstration evidence; not a live research claim."],
            )
            claim = EvidenceClaim(
                claim_id=f"demo-claim-{name}",
                claim=f"{symbol} is included to demonstrate evidence-linked policy review.",
                source_ids=[source.source_id],
                stance=EvidenceStance.CONTEXT,
                confidence=Decimal("0.60"),
                caveats=["No investment conclusion should be drawn from this fixture."],
            )
            brief = ResearchBrief(
                research_id=_id("research", name),
                summary=f"Point-in-time seeded research record for {symbol}.",
                as_of=created_at,
                sources=[source],
                claims=[claim],
                caveats=["Credential-free demo data."],
                researcher_prompt_version="researcher-v1",
            )
            proposal = TradeProposal(
                proposal_id=_id("proposal", name),
                account_name=name,
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                sector="technology" if symbol != "COIN" else "financials",
                rationale=f"Seeded paper proposal for {symbol}; no real order is placed.",
                evidence_claim_ids=[claim.claim_id],
                created_at=created_at,
                research=brief,
                market_observation=observation,
            )
            outcome = RiskOutcome.APPROVED if approved else RiskOutcome.REJECTED
            rule = RiskRuleResult(
                rule="maximum_order_notional",
                passed=approved,
                reason=(
                    "seeded order remains within the configured paper limit"
                    if approved
                    else "seeded order exceeds the configured paper limit"
                ),
            )
            decision = RiskDecision(
                decision_id=_id("decision", name),
                proposal_id=proposal.proposal_id,
                account_name=name,
                outcome=outcome,
                evaluated_at=created_at + timedelta(seconds=1),
                rules=[rule],
            )
            account = _account_payload(name, strategy, proposal, approved)
            conn.execute(
                "INSERT INTO accounts(name, account) VALUES (?, ?)", (name, json.dumps(account))
            )
            conn.execute(
                "INSERT INTO research_briefs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(brief.research_id),
                    name,
                    brief.model_dump_json(),
                    brief.as_of.isoformat(),
                    brief.researcher_prompt_version,
                    "trader-v1",
                    created_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO trade_proposals VALUES (?, ?, ?, ?, ?)",
                (
                    str(proposal.proposal_id),
                    name,
                    proposal.model_dump_json(),
                    created_at.isoformat(),
                    str(brief.research_id),
                ),
            )
            conn.execute(
                "INSERT INTO market_observations VALUES (?, ?, 'proposal', ?, ?, ?, ?)",
                (
                    f"demo-proposal:{proposal.proposal_id}",
                    name,
                    str(proposal.proposal_id),
                    symbol,
                    observation.model_dump_json(),
                    created_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO risk_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(decision.decision_id),
                    str(proposal.proposal_id),
                    name,
                    outcome.value,
                    decision.model_dump_json(),
                    decision.evaluated_at.isoformat(),
                ),
            )
            if approved:
                order = PaperOrder(
                    order_id=_id("order", name),
                    decision_id=decision.decision_id,
                    proposal_id=proposal.proposal_id,
                    account_name=name,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=quantity,
                    observation=observation,
                    submitted_at=created_at + timedelta(seconds=2),
                )
                execution_price = observation.price * (Decimal("1") + Decimal(str(SPREAD)))
                result = ExecutionResult(
                    execution_id=order.order_id,
                    order_id=order.order_id,
                    status=ExecutionStatus.EXECUTED,
                    executed_at=created_at + timedelta(seconds=3),
                    quantity=quantity,
                    execution_price=execution_price,
                    cash_after=Decimal(str(account["balance"])),
                    message="seeded paper order executed",
                )
                conn.execute(
                    "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(order.order_id),
                        str(order.decision_id),
                        str(order.proposal_id),
                        name,
                        order.model_dump_json(),
                        ExecutionStatus.EXECUTED.value,
                        order.submitted_at.isoformat(),
                    ),
                )
                conn.execute(
                    "INSERT INTO execution_results VALUES (?, ?, ?, ?)",
                    (
                        str(result.execution_id),
                        str(result.order_id),
                        result.model_dump_json(),
                        result.executed_at.isoformat(),
                    ),
                )
                conn.execute(
                    "INSERT INTO market_observations VALUES (?, ?, 'order', ?, ?, ?, ?)",
                    (
                        f"demo-order:{order.order_id}",
                        name,
                        str(order.order_id),
                        symbol,
                        observation.model_dump_json(),
                        result.executed_at.isoformat(),
                    ),
                )
            conn.execute(
                "INSERT INTO logs(name, datetime, type, message) VALUES (?, ?, ?, ?)",
                (
                    name,
                    created_at.isoformat(),
                    "account",
                    f"Seeded {outcome.value} paper proposal for {symbol}",
                ),
            )
            cycle_id = str(_id("cycle", name))
            conn.execute(
                """INSERT INTO cycle_metrics
                   (cycle_id, account_name, run_id, scenario_id, model, prompt_version,
                    market_mode, started_at, completed_at, status, requests, input_tokens,
                    output_tokens, total_tokens, estimated_cost_usd, latency_ms,
                    error_summary, decision_ids, budget)
                   VALUES (?, ?, ?, NULL, 'offline-demo-proxy', 'trader-v1', 'simulated',
                           ?, ?, 'succeeded', 2, 900, 240, 1140, '0', ?, NULL, ?, ?)""",
                (
                    cycle_id,
                    name,
                    f"demo-run-{DEMO_SEED_VERSION}",
                    (created_at - timedelta(seconds=1)).isoformat(),
                    (created_at + timedelta(seconds=3)).isoformat(),
                    110 + offset * 17,
                    json.dumps([str(decision.decision_id)]),
                    CycleBudget().model_dump_json(),
                ),
            )
            conn.execute(
                "INSERT INTO decision_telemetry VALUES (?, ?, ?, ?)",
                (
                    str(decision.decision_id),
                    cycle_id,
                    f"trace_demo_{name}",
                    created_at.isoformat(),
                ),
            )
        for name, latency in (
            ("paper-accounts", 3.1),
            ("notifications", 2.4),
            ("market-data", 4.2),
            ("research-search", 18.4),
        ):
            conn.execute(
                """INSERT INTO service_health
                   (name, state, required, last_success, latency_ms, consecutive_failures,
                    attempt_count, failure_count, active)
                   VALUES (?, 'healthy', 1, ?, ?, 0, 1, 0, 1)
                   ON CONFLICT(name) DO UPDATE SET state='healthy', required=1,
                       last_success=excluded.last_success, latency_ms=excluded.latency_ms,
                       consecutive_failures=0, error_summary=NULL,
                       circuit_open_until=NULL, attempt_count=MAX(attempt_count, 1), active=1""",
                (name, DEMO_AS_OF.isoformat(), latency),
            )
        conn.execute(
            """UPDATE service_health SET state='healthy', last_success=?,
               error_summary=NULL, consecutive_failures=0, circuit_open_until=NULL,
               latency_ms=COALESCE(latency_ms, 5), attempt_count=MAX(attempt_count, 1)""",
            (DEMO_AS_OF.isoformat(),),
        )
        conn.execute(
            "INSERT INTO demo_seed_state(version, seeded_at) VALUES (?, ?)",
            (DEMO_SEED_VERSION, datetime.now(timezone.utc).isoformat()),
        )
    return True


def main() -> None:
    import os

    path = Path(os.getenv("ACCOUNTS_DB", "accounts.db"))
    inserted = seed_demo_database(path)
    print(f"{'seeded' if inserted else 'already seeded'} {path}")


if __name__ == "__main__":
    main()
