"""SQLite persistence for the auditable proposal-to-execution chain."""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from backend import database
from backend.accounts import INITIAL_BALANCE, SPREAD, Transaction
from backend.market import MarketObservation
from backend.research import ResearchBrief

from .models import (
    ExecutionResult,
    ExecutionStatus,
    OrderSide,
    PaperOrder,
    RiskDecision,
    RiskOutcome,
    TradeProposal,
)


class ExecutionConflict(RuntimeError):
    pass


class DecisionRepository:
    def __init__(self, path: str | None = None):
        self.path = path
        database.initialize_database(path)

    @property
    def db_path(self) -> str:
        return self.path or database.DB

    def save_research_brief(
        self,
        account_name: str,
        brief: ResearchBrief,
        trader_prompt_version: str,
        created_at: datetime,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO research_briefs
                   (research_id, account_name, brief, decision_cutoff, researcher_prompt_version,
                    trader_prompt_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(research_id) DO NOTHING""",
                (
                    str(brief.research_id),
                    account_name.lower(),
                    brief.model_dump_json(),
                    brief.as_of.isoformat(),
                    brief.researcher_prompt_version,
                    trader_prompt_version,
                    created_at.isoformat(),
                ),
            )
            stored = conn.execute(
                """SELECT account_name, brief, researcher_prompt_version, trader_prompt_version
                   FROM research_briefs WHERE research_id = ?""",
                (str(brief.research_id),),
            ).fetchone()
            expected = (
                account_name.lower(),
                brief.model_dump_json(),
                brief.researcher_prompt_version,
                trader_prompt_version,
            )
            if stored != expected:
                raise ExecutionConflict(
                    "research_id already exists with different evidence or prompt versions"
                )

    def save_proposal(self, proposal: TradeProposal) -> None:
        observation_id = f"proposal:{proposal.proposal_id}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO trade_proposals
                   (proposal_id, account_name, proposal, created_at, research_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(proposal.proposal_id),
                    proposal.account_name,
                    proposal.model_dump_json(),
                    proposal.created_at.isoformat(),
                    str(proposal.research.research_id),
                ),
            )
            conn.execute(
                """INSERT INTO market_observations
                   (id, account_name, usage_kind, related_id, symbol, observation, recorded_at)
                   VALUES (?, ?, 'proposal', ?, ?, ?, ?)""",
                (
                    observation_id,
                    proposal.account_name,
                    str(proposal.proposal_id),
                    proposal.symbol,
                    proposal.market_observation.model_dump_json(),
                    proposal.created_at.isoformat(),
                ),
            )

    def save_risk_decision(self, decision: RiskDecision) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO risk_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(decision.decision_id),
                    str(decision.proposal_id),
                    decision.account_name,
                    decision.outcome.value,
                    decision.model_dump_json(),
                    decision.evaluated_at.isoformat(),
                ),
            )

    def approve_human(self, decision_id: str, approved_at: datetime) -> RiskDecision:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT decision FROM risk_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown decision {decision_id}")
            decision = RiskDecision.model_validate_json(row[0])
            if decision.outcome != RiskOutcome.PENDING_HUMAN:
                raise ExecutionConflict("decision is not awaiting human approval")
            decision = decision.model_copy(
                update={
                    "outcome": RiskOutcome.APPROVED,
                    "human_approved_at": approved_at,
                }
            )
            conn.execute(
                "UPDATE risk_decisions SET outcome = ?, decision = ? WHERE decision_id = ?",
                (decision.outcome.value, decision.model_dump_json(), decision_id),
            )
            return decision

    def daily_turnover(self, account_name: str, day: str) -> Decimal:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT er.result FROM execution_results er
                   JOIN paper_orders po ON po.order_id = er.order_id
                   WHERE po.account_name = ? AND substr(er.executed_at, 1, 10) = ?""",
                (account_name.lower(), day),
            ).fetchall()
        return sum(
            (ExecutionResult.model_validate_json(row[0]).execution_price or Decimal("0"))
            * ExecutionResult.model_validate_json(row[0]).quantity
            for row in rows
        )

    def load_proposal(self, proposal_id: str) -> TradeProposal:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT proposal FROM trade_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown proposal {proposal_id}")
        return TradeProposal.model_validate_json(row[0])

    def load_risk_decision(self, decision_id: str) -> RiskDecision:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT decision FROM risk_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown decision {decision_id}")
        return RiskDecision.model_validate_json(row[0])

    def evidence_chain(self, proposal_id: str) -> dict:
        """Return only persisted concise evidence and deterministic decision artifacts."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT rb.brief, rb.researcher_prompt_version, rb.trader_prompt_version,
                          tp.proposal, rd.decision, po.order_payload, er.result
                   FROM trade_proposals tp
                   JOIN research_briefs rb ON rb.research_id = tp.research_id
                   LEFT JOIN risk_decisions rd ON rd.proposal_id = tp.proposal_id
                   LEFT JOIN paper_orders po ON po.proposal_id = tp.proposal_id
                   LEFT JOIN execution_results er ON er.order_id = po.order_id
                   WHERE tp.proposal_id = ?""",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown proposal {proposal_id}")
        proposal = json.loads(row[3])
        return {
            "research": json.loads(row[0]),
            "prompt_versions": {"researcher": row[1], "trader": row[2]},
            "proposal": proposal,
            "market_observation": proposal["market_observation"],
            "risk_decision": json.loads(row[4]) if row[4] else None,
            "order": json.loads(row[5]) if row[5] else None,
            "execution": json.loads(row[6]) if row[6] else None,
        }

    def load_account_data(self, account_name: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT account FROM accounts WHERE name = ?", (account_name.lower(),)
            ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "name": account_name.lower(),
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": [],
            }
        )

    def execute_atomic(
        self,
        order: PaperOrder,
        rationale: str,
        fail_after_account_write: bool = False,
        executed_at: datetime | None = None,
    ) -> ExecutionResult:
        """Execute once while account, observation, transaction, order, and result share a transaction."""
        now = executed_at or datetime.now(timezone.utc)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT result FROM execution_results WHERE order_id = ?",
                (str(order.order_id),),
            ).fetchone()
            if duplicate:
                existing = ExecutionResult.model_validate_json(duplicate[0])
                return existing.model_copy(
                    update={
                        "status": ExecutionStatus.DUPLICATE,
                        "message": "order already executed",
                    }
                )
            decision_row = conn.execute(
                "SELECT outcome, decision FROM risk_decisions WHERE decision_id = ? AND proposal_id = ?",
                (str(order.decision_id), str(order.proposal_id)),
            ).fetchone()
            if decision_row is None:
                raise ExecutionConflict("no persisted risk decision for order")
            decision = RiskDecision.model_validate_json(decision_row[1])
            if decision.outcome != RiskOutcome.APPROVED:
                raise ExecutionConflict("execution requires a persisted approval")
            proposal_row = conn.execute(
                "SELECT proposal FROM trade_proposals WHERE proposal_id = ?",
                (str(order.proposal_id),),
            ).fetchone()
            if proposal_row is None:
                raise ExecutionConflict("no persisted proposal for order")
            proposal = TradeProposal.model_validate_json(proposal_row[0])
            if (
                decision.requested_quantity is not None
                and decision.requested_quantity != proposal.quantity
            ):
                raise ExecutionConflict("approved decision does not match requested quantity")
            approved_quantity = decision.approved_quantity or proposal.quantity
            observation_row = conn.execute(
                """SELECT observation FROM market_observations
                   WHERE usage_kind = 'proposal' AND related_id = ?""",
                (str(order.proposal_id),),
            ).fetchone()
            if (
                observation_row is None
                or MarketObservation.model_validate_json(observation_row[0])
                != proposal.market_observation
            ):
                raise ExecutionConflict("execution requires the persisted proposal observation")
            if order.observation != proposal.market_observation:
                raise ExecutionConflict("order observation differs from approved proposal")
            if (
                order.account_name != proposal.account_name
                or order.symbol != proposal.symbol
                or order.side != proposal.side
                or order.quantity != approved_quantity
            ):
                raise ExecutionConflict("order terms differ from approved proposal")

            account_row = conn.execute(
                "SELECT account FROM accounts WHERE name = ?", (order.account_name,)
            ).fetchone()
            account = (
                json.loads(account_row[0])
                if account_row
                else {
                    "name": order.account_name,
                    "balance": INITIAL_BALANCE,
                    "strategy": "",
                    "holdings": {},
                    "transactions": [],
                    "portfolio_value_time_series": [],
                }
            )
            cash = Decimal(str(account["balance"]))
            price = (
                order.observation.price * (Decimal("1") + Decimal(str(SPREAD)))
                if order.side == OrderSide.BUY
                else order.observation.price * (Decimal("1") - Decimal(str(SPREAD)))
            )
            total = price * order.quantity
            held = int(account["holdings"].get(order.symbol, 0))
            if order.side == OrderSide.BUY:
                if total > cash:
                    raise ExecutionConflict("insufficient cash at execution")
                cash -= total
                account["holdings"][order.symbol] = held + order.quantity
                signed_quantity = order.quantity
            else:
                if held < order.quantity:
                    raise ExecutionConflict("insufficient holdings at execution")
                cash += total
                remaining = held - order.quantity
                if remaining:
                    account["holdings"][order.symbol] = remaining
                else:
                    account["holdings"].pop(order.symbol, None)
                signed_quantity = -order.quantity
            observation_id = f"order:{order.order_id}"
            transaction = Transaction(
                symbol=order.symbol,
                quantity=signed_quantity,
                price=float(price),
                timestamp=now.isoformat(),
                rationale=rationale,
                market_observation_id=observation_id,
                market_observation=order.observation,
            )
            account["balance"] = float(cash)
            account["transactions"].append(transaction.model_dump(mode="json"))
            conn.execute(
                """INSERT INTO accounts(name, account) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET account=excluded.account""",
                (order.account_name, json.dumps(account)),
            )
            if fail_after_account_write:
                raise RuntimeError("injected execution failure")
            conn.execute(
                """INSERT INTO market_observations
                   (id, account_name, usage_kind, related_id, symbol, observation, recorded_at)
                   VALUES (?, ?, 'order', ?, ?, ?, ?)""",
                (
                    observation_id,
                    order.account_name,
                    str(order.order_id),
                    order.symbol,
                    order.observation.model_dump_json(),
                    now.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(order.order_id),
                    str(order.decision_id),
                    str(order.proposal_id),
                    order.account_name,
                    order.model_dump_json(),
                    ExecutionStatus.EXECUTED.value,
                    order.submitted_at.isoformat(),
                ),
            )
            result = ExecutionResult(
                execution_id=order.order_id,
                order_id=order.order_id,
                status=ExecutionStatus.EXECUTED,
                executed_at=now,
                quantity=order.quantity,
                execution_price=price,
                cash_after=cash,
                message="paper order executed",
            )
            conn.execute(
                "INSERT INTO execution_results VALUES (?, ?, ?, ?)",
                (
                    str(result.execution_id),
                    str(order.order_id),
                    result.model_dump_json(),
                    now.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO logs(name, datetime, type, message) VALUES (?, ?, 'account', ?)",
                (
                    order.account_name,
                    now.isoformat(),
                    f"{order.side.value.title()} {order.quantity} of {order.symbol} (paper)",
                ),
            )
            return result

    def audit_chain(self, account_name: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT tp.proposal, rd.decision, po.order_payload, er.result
                   FROM trade_proposals tp
                   LEFT JOIN risk_decisions rd ON rd.proposal_id = tp.proposal_id
                   LEFT JOIN paper_orders po ON po.proposal_id = tp.proposal_id
                   LEFT JOIN execution_results er ON er.order_id = po.order_id
                   WHERE tp.account_name = ? ORDER BY tp.created_at""",
                (account_name.lower(),),
            ).fetchall()
        return [
            {
                "proposal": json.loads(row[0]),
                "risk_decision": json.loads(row[1]) if row[1] else None,
                "order": json.loads(row[2]) if row[2] else None,
                "execution": json.loads(row[3]) if row[3] else None,
            }
            for row in rows
        ]
