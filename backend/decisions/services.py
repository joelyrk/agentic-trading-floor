"""Separated proposal, approval, and execution application services."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from backend.market import MarketDataError, MarketObservation
from backend.research import ResearchPolicy, ResearchPolicyError
from backend.research.models import TRADER_PROMPT_VERSION

from .models import (
    ExecutionResult,
    PaperOrder,
    ProposedTrade,
    ResearchBrief,
    RiskDecision,
    RiskOutcome,
    TradeProposal,
    TraderRecommendation,
    TradingDecision,
    validate_proposal_evidence,
)
from .repository import DecisionRepository, ExecutionConflict
from .risk import PortfolioSnapshot, RiskEngine

Clock = Callable[[], datetime]
Observe = Callable[[str], MarketObservation]


class ProposalService:
    def __init__(
        self,
        repository: DecisionRepository,
        observe: Observe,
        clock: Clock | None = None,
        research_policy: ResearchPolicy | None = None,
    ):
        self.repository = repository
        self.observe = observe
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.research_policy = research_policy or ResearchPolicy.from_env()

    def create(
        self,
        account_name: str,
        proposed: ProposedTrade,
        research: ResearchBrief,
        trader_prompt_version: str = TRADER_PROMPT_VERSION,
    ) -> TradeProposal:
        self.record_research(account_name, research, trader_prompt_version)
        observation = self.observe(proposed.symbol)
        now = self.clock()
        proposal = TradeProposal(
            **proposed.model_dump(),
            account_name=account_name,
            created_at=now,
            research=research,
            market_observation=observation,
        )
        self.repository.save_proposal(proposal)
        return proposal

    def record_research(
        self,
        account_name: str,
        research: ResearchBrief,
        trader_prompt_version: str,
    ) -> datetime:
        self.research_policy.validate(research)
        now = self.clock()
        self.repository.save_research_brief(account_name, research, trader_prompt_version, now)
        return now


class RiskService:
    def __init__(
        self,
        repository: DecisionRepository,
        engine: RiskEngine,
        observe: Observe,
        clock: Clock | None = None,
    ):
        self.repository = repository
        self.engine = engine
        self.observe = observe
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _snapshot(self, proposal: TradeProposal) -> PortfolioSnapshot:
        data = self.repository.load_account_data(proposal.account_name)
        prices: dict[str, Decimal] = {}
        for symbol in data["holdings"]:
            prices[symbol] = (
                proposal.market_observation.price
                if symbol == proposal.symbol
                else self.observe(symbol).price
            )
        prices.setdefault(proposal.symbol, proposal.market_observation.price)
        series = data.get("portfolio_value_time_series", [])
        historic_values = [Decimal(str(item[1])) for item in series]
        current_value = Decimal(str(data["balance"])) + sum(
            prices[symbol] * quantity for symbol, quantity in data["holdings"].items()
        )
        peak = max(historic_values + [current_value]) if historic_values else current_value
        sector_map = {
            symbol: self.engine.policy.sector_by_symbol.get(symbol, "unclassified")
            for symbol in set(data["holdings"]) | {proposal.symbol}
        }
        return PortfolioSnapshot(
            cash=Decimal(str(data["balance"])),
            holdings=dict(data["holdings"]),
            prices=prices,
            sectors=sector_map,
            daily_turnover=self.repository.daily_turnover(
                proposal.account_name, self.clock().date().isoformat()
            ),
            peak_value=peak,
        )

    def evaluate(self, proposal: TradeProposal) -> RiskDecision:
        now = self.clock()
        decision = self.engine.evaluate(proposal, self._snapshot(proposal), now)
        self.repository.save_risk_decision(decision)
        return decision

    def human_approve(self, decision_id: str) -> RiskDecision:
        return self.repository.approve_human(decision_id, self.clock())


class ExecutionService:
    def __init__(self, repository: DecisionRepository, clock: Clock | None = None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, proposal: TradeProposal, decision: RiskDecision) -> ExecutionResult | None:
        if decision.outcome != RiskOutcome.APPROVED:
            return None
        approved_quantity = decision.approved_quantity or proposal.quantity
        order_id = uuid5(NAMESPACE_URL, f"order:{decision.decision_id}")
        order = PaperOrder(
            order_id=order_id,
            decision_id=decision.decision_id,
            proposal_id=proposal.proposal_id,
            account_name=proposal.account_name,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=approved_quantity,
            observation=proposal.market_observation,
            submitted_at=self.clock(),
        )
        return self.repository.execute_atomic(order, proposal.rationale, executed_at=self.clock())


class DecisionPipeline:
    def __init__(
        self,
        proposal_service: ProposalService,
        risk_service: RiskService,
        execution_service: ExecutionService,
    ):
        self.proposal_service = proposal_service
        self.risk_service = risk_service
        self.execution_service = execution_service

    def process(
        self, account_name: str, output: TradingDecision
    ) -> list[tuple[TradeProposal, RiskDecision, ExecutionResult | None]]:
        results = []
        if not output.proposals:
            self.proposal_service.record_research(
                account_name, output.research, output.trader_prompt_version
            )
        for proposed in output.proposals:
            proposal = self.proposal_service.create(
                account_name, proposed, output.research, output.trader_prompt_version
            )
            decision = self.risk_service.evaluate(proposal)
            execution = self.execution_service.execute(proposal, decision)
            results.append((proposal, decision, execution))
        return results

    def safely_process(self, account_name: str, raw_output) -> tuple[list, str | None]:
        """Validate output and isolate attributable failures between paper proposals."""
        try:
            output = (
                raw_output
                if isinstance(raw_output, TradingDecision)
                else TradingDecision.model_validate(raw_output)
            )
        except (ValidationError, TypeError, ValueError, ResearchPolicyError) as exc:
            return [], f"invalid_agent_output: {exc}"
        if not output.proposals:
            try:
                self.proposal_service.record_research(
                    account_name, output.research, output.trader_prompt_version
                )
            except ResearchPolicyError as exc:
                return [], f"research_policy_rejection: {exc}"
            return [], None

        results = []
        errors: list[str] = []
        for proposed in output.proposals:
            try:
                proposal = self.proposal_service.create(
                    account_name,
                    proposed,
                    output.research,
                    output.trader_prompt_version,
                )
                decision = self.risk_service.evaluate(proposal)
                execution = self.execution_service.execute(proposal, decision)
                results.append((proposal, decision, execution))
            except ResearchPolicyError as exc:
                errors.append(f"{proposed.symbol}: research_policy_rejection: {exc}")
            except ExecutionConflict as exc:
                errors.append(f"{proposed.symbol}: execution_conflict: {exc}")
            except MarketDataError as exc:
                errors.append(f"{proposed.symbol}: market_data_unavailable: {exc}")
        return results, "; ".join(errors) or None

    def safely_process_recommendation(
        self,
        account_name: str,
        research: ResearchBrief,
        recommendation: TraderRecommendation,
        trader_prompt_version: str = TRADER_PROMPT_VERSION,
    ) -> tuple[list, str | None]:
        """Persist research once, then reject bad evidence references proposal by proposal."""
        try:
            self.proposal_service.record_research(account_name, research, trader_prompt_version)
        except ResearchPolicyError as exc:
            return [], f"research_policy_rejection: {exc}"

        results = []
        errors: list[str] = []
        for proposed in recommendation.proposals:
            try:
                validate_proposal_evidence(research, proposed)
            except ValueError as exc:
                errors.append(f"{proposed.symbol}: evidence_rejection: {exc}")
                continue
            try:
                proposal = self.proposal_service.create(
                    account_name,
                    proposed,
                    research,
                    trader_prompt_version,
                )
                decision = self.risk_service.evaluate(proposal)
                execution = self.execution_service.execute(proposal, decision)
                results.append((proposal, decision, execution))
            except ResearchPolicyError as exc:
                errors.append(f"{proposed.symbol}: research_policy_rejection: {exc}")
            except ExecutionConflict as exc:
                errors.append(f"{proposed.symbol}: execution_conflict: {exc}")
            except MarketDataError as exc:
                errors.append(f"{proposed.symbol}: market_data_unavailable: {exc}")
        return results, "; ".join(errors) or None
