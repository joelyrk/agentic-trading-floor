import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.decisions import (
    DecisionPipeline,
    EvidenceClaim,
    ExecutionService,
    ExecutionStatus,
    OrderSide,
    ProposalService,
    ResearchBrief,
    RiskEngine,
    RiskOutcome,
    RiskPolicy,
    RiskService,
    SourceRecord,
    TradingDecision,
)
from backend.decisions.models import ProposedTrade
from backend.decisions.repository import DecisionRepository, ExecutionConflict
from backend.market import MarketDataError
from backend.market.models import DataMode, MarketObservation, ObservationSource

NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def observe(symbol: str) -> MarketObservation:
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


def policy(**updates) -> RiskPolicy:
    data = dict(
        max_position_percentage=Decimal("1"),
        max_symbol_concentration=Decimal("1"),
        max_sector_concentration=Decimal("1"),
        minimum_cash_reserve=Decimal("0"),
        maximum_order_notional=Decimal("100000"),
        maximum_daily_turnover=Decimal("100000"),
        maximum_drawdown=Decimal("1"),
    )
    data.update(updates)
    return RiskPolicy(**data)


def output(quantity=2, side=OrderSide.BUY) -> TradingDecision:
    source = SourceRecord(
        source_id="source-1",
        canonical_url="https://example.com/aapl?utm_source=test",
        publisher="Example News",
        title="Apple update",
        published_at=NOW,
        retrieved_at=NOW,
        supporting_excerpt="Apple published a relevant operating update.",
    )
    claim = EvidenceClaim(
        claim_id="claim-1",
        claim="Apple has a current operating update.",
        source_ids=[source.source_id],
        stance="supports",
        confidence=Decimal("0.8"),
    )
    return TradingDecision(
        research=ResearchBrief(summary="evidence", as_of=NOW, sources=[source], claims=[claim]),
        appraisal="paper proposal only",
        proposals=[
            ProposedTrade(
                symbol="AAPL",
                side=side,
                quantity=quantity,
                sector="Technology",
                rationale="test",
                evidence_claim_ids=[claim.claim_id],
            )
        ],
    )


def services(tmp_path, risk_policy=None):
    repo = DecisionRepository(str(tmp_path / "accounts.db"))
    proposals = ProposalService(repo, observe, clock=lambda: NOW)
    risks = RiskService(repo, RiskEngine(risk_policy or policy()), observe, clock=lambda: NOW)
    executions = ExecutionService(repo, clock=lambda: NOW)
    return repo, proposals, risks, executions


def test_pipeline_persists_auditable_chain_and_exact_observation(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path)
    result = DecisionPipeline(proposals, risks, executions).process("Alice", output())[0]
    proposal, decision, execution = result
    assert decision.outcome == RiskOutcome.APPROVED
    assert execution and execution.status == ExecutionStatus.EXECUTED
    chain = repo.audit_chain("alice")
    assert len(chain) == 1
    assert chain[0]["proposal"]["proposal_id"] == str(proposal.proposal_id)
    assert chain[0]["risk_decision"]["decision_id"] == str(decision.decision_id)
    assert chain[0]["execution"]["order_id"] == str(execution.order_id)
    with sqlite3.connect(repo.db_path) as conn:
        usages = conn.execute(
            "SELECT usage_kind FROM market_observations ORDER BY usage_kind"
        ).fetchall()
    assert usages == [("order",), ("proposal",)]
    account = repo.load_account_data("alice")
    assert account["holdings"] == {"AAPL": 2}
    assert account["balance"] == pytest.approx(9799.6)


def test_pipeline_persists_requested_size_and_executes_only_approved_size(tmp_path) -> None:
    repo, proposals, risks, executions = services(
        tmp_path, policy(maximum_order_notional=Decimal("250.5"))
    )
    proposal, decision, execution = DecisionPipeline(proposals, risks, executions).process(
        "Alice", output(quantity=50)
    )[0]

    assert proposal.quantity == 50
    assert decision.requested_quantity == 50
    assert decision.approved_quantity == 2
    assert execution and execution.quantity == 2
    chain = repo.audit_chain("alice")[0]
    assert chain["proposal"]["quantity"] == 50
    assert chain["risk_decision"]["requested_quantity"] == 50
    assert chain["risk_decision"]["approved_quantity"] == 2
    assert chain["order"]["quantity"] == 2
    assert repo.load_account_data("alice")["holdings"] == {"AAPL": 2}


def test_execution_rejects_quantity_above_persisted_approved_size(tmp_path) -> None:
    repo, proposals, risks, _ = services(
        tmp_path, policy(maximum_order_notional=Decimal("250.5"))
    )
    proposal = proposals.create("Alice", output(quantity=50).proposals[0], output().research)
    decision = risks.evaluate(proposal)
    from uuid import NAMESPACE_URL, uuid5

    from backend.decisions import PaperOrder

    order = PaperOrder(
        order_id=uuid5(NAMESPACE_URL, f"order:{decision.decision_id}"),
        decision_id=decision.decision_id,
        proposal_id=proposal.proposal_id,
        account_name="alice",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=proposal.quantity,
        observation=proposal.market_observation,
        submitted_at=NOW,
    )
    with pytest.raises(ExecutionConflict, match="order terms differ"):
        repo.execute_atomic(order, proposal.rationale)


def test_rejection_is_persisted_and_never_executes(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path, policy(allowed_universe={"MSFT"}))
    proposal, decision, execution = DecisionPipeline(proposals, risks, executions).process(
        "Alice", output()
    )[0]
    assert decision.outcome == RiskOutcome.REJECTED
    assert execution is None
    chain = repo.audit_chain("alice")[0]
    assert chain["risk_decision"]["rules"]
    assert chain["order"] is None
    assert repo.load_account_data("alice")["holdings"] == {}


def test_duplicate_and_concurrent_execution_are_idempotent(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path)
    proposal = proposals.create("Alice", output().proposals[0], output().research)
    decision = risks.evaluate(proposal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: executions.execute(proposal, decision), range(2)))
    assert {result.status for result in results if result} == {
        ExecutionStatus.EXECUTED,
        ExecutionStatus.DUPLICATE,
    }
    assert repo.load_account_data("alice")["holdings"] == {"AAPL": 2}
    assert len(repo.audit_chain("alice")) == 1


def test_atomic_rollback_after_account_write(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path)
    proposal = proposals.create("Alice", output().proposals[0], output().research)
    decision = risks.evaluate(proposal)
    from uuid import NAMESPACE_URL, uuid5

    from backend.decisions import PaperOrder

    order = PaperOrder(
        order_id=uuid5(NAMESPACE_URL, f"order:{decision.decision_id}"),
        decision_id=decision.decision_id,
        proposal_id=proposal.proposal_id,
        account_name="alice",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=2,
        observation=proposal.market_observation,
        submitted_at=NOW,
    )
    with pytest.raises(RuntimeError, match="injected"):
        repo.execute_atomic(order, "test", fail_after_account_write=True)
    assert repo.load_account_data("alice")["holdings"] == {}
    assert repo.audit_chain("alice")[0]["order"] is None


def test_execution_requires_persisted_approval(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path, policy(allowed_universe={"MSFT"}))
    proposal = proposals.create("Alice", output().proposals[0], output().research)
    decision = risks.evaluate(proposal)
    assert decision.outcome == RiskOutcome.REJECTED
    assert executions.execute(proposal, decision) is None


def test_human_approval_is_explicit_and_disabled_for_execution_until_recorded(
    tmp_path,
) -> None:
    human_policy = policy(
        human_approval_enabled=True,
        human_approval_notional=Decimal("100"),
        automated_replay=False,
    )
    repo, proposals, risks, executions = services(tmp_path, human_policy)
    proposal = proposals.create("Alice", output().proposals[0], output().research)
    decision = risks.evaluate(proposal)
    assert decision.outcome == RiskOutcome.PENDING_HUMAN
    assert executions.execute(proposal, decision) is None
    approved = risks.human_approve(str(decision.decision_id))
    assert approved.outcome == RiskOutcome.APPROVED
    assert approved.human_approved_at == NOW
    assert executions.execute(proposal, approved).status == ExecutionStatus.EXECUTED


@pytest.mark.parametrize("race", ["cash", "holdings"])
def test_execution_rechecks_cash_and_holdings_after_approval(tmp_path, race) -> None:
    side = OrderSide.BUY if race == "cash" else OrderSide.SELL
    repo, proposals, risks, executions = services(tmp_path)
    initial = repo.load_account_data("alice")
    if side == OrderSide.SELL:
        initial["holdings"] = {"AAPL": 2}
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "INSERT INTO accounts(name, account) VALUES ('alice', ?)",
            (json.dumps(initial),),
        )
    proposal = proposals.create("Alice", output(side=side).proposals[0], output(side=side).research)
    decision = risks.evaluate(proposal)
    assert decision.outcome == RiskOutcome.APPROVED
    raced = repo.load_account_data("alice")
    if race == "cash":
        raced["balance"] = 0
    else:
        raced["holdings"] = {}
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE accounts SET account = ? WHERE name = 'alice'", (json.dumps(raced),))
    with pytest.raises(ExecutionConflict, match="insufficient"):
        executions.execute(proposal, decision)
    assert repo.audit_chain("alice")[0]["order"] is None


def test_execution_requires_persisted_market_observation(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path)
    proposal = proposals.create("Alice", output().proposals[0], output().research)
    decision = risks.evaluate(proposal)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "DELETE FROM market_observations WHERE related_id = ?",
            (str(proposal.proposal_id),),
        )
    with pytest.raises(ExecutionConflict, match="persisted proposal observation"):
        executions.execute(proposal, decision)


def test_malformed_agent_output_is_safely_rejected(tmp_path) -> None:
    repo, proposals, risks, executions = services(tmp_path)
    processed, error = DecisionPipeline(proposals, risks, executions).safely_process(
        "Alice",
        {
            "research": {"summary": "x", "as_of": NOW},
            "appraisal": "x",
            "proposals": [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 1.5,
                    "sector": "tech",
                    "rationale": "x",
                    "evidence_claim_ids": ["missing"],
                }
            ],
        },
    )
    assert processed == []
    assert error and error.startswith("invalid_agent_output")
    assert repo.audit_chain("alice") == []


def test_pipeline_preserves_completed_proposals_when_later_market_data_fails() -> None:
    class Proposals:
        def create(self, account_name, proposed, research, trader_prompt_version):
            return proposed

    class Risks:
        def evaluate(self, proposal):
            if proposal.symbol == "BEAM":
                raise MarketDataError("provider_error: Massive request failed")
            return f"approved-{proposal.symbol}"

    class Executions:
        def execute(self, proposal, decision):
            return f"executed-{proposal.symbol}"

    decision = output().model_copy(
        update={
            "proposals": [
                output().proposals[0].model_copy(update={"symbol": symbol})
                for symbol in ("COIN", "RBLX", "BEAM")
            ]
        }
    )
    processed, error = DecisionPipeline(Proposals(), Risks(), Executions()).safely_process(
        "Cathie", decision
    )
    assert [proposal.symbol for proposal, _, _ in processed] == ["COIN", "RBLX"]
    assert error == "BEAM: market_data_unavailable: provider_error: Massive request failed"


def test_positive_integral_quantity_schema() -> None:
    with pytest.raises(ValidationError):
        ProposedTrade(
            symbol="AAPL",
            side="buy",
            quantity=1.5,
            sector="tech",
            rationale="x",
            evidence_claim_ids=["claim-1"],
        )
