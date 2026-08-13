"""Typed contracts at every decision and execution boundary."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.market.models import MarketObservation, normalize_symbol
from backend.research.models import ResearchBrief, TRADER_PROMPT_VERSION


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class RiskOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING_HUMAN = "pending_human"


class ExecutionStatus(StrEnum):
    EXECUTED = "executed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class ProposedTrade(StrictModel):
    """Model-owned fields. IDs and observations are assigned by deterministic code."""

    symbol: str
    side: OrderSide
    quantity: Annotated[int, Field(strict=True, gt=0)]
    sector: str = Field(min_length=1, max_length=80)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_claim_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("sector", mode="before")
    @classmethod
    def normalize_sector(cls, value: str) -> str:
        return value.strip().lower()


class TradingDecision(StrictModel):
    """Validated structured output returned by a trader agent."""

    research: ResearchBrief
    proposals: list[ProposedTrade] = Field(default_factory=list, max_length=20)
    appraisal: str = Field(min_length=1, max_length=2000)
    trader_prompt_version: str = Field(default=TRADER_PROMPT_VERSION, min_length=1, max_length=100)

    @model_validator(mode="after")
    def proposals_reference_supported_claims(self) -> "TradingDecision":
        claims = {claim.claim_id: claim for claim in self.research.claims}
        for proposal in self.proposals:
            broken = set(proposal.evidence_claim_ids) - claims.keys()
            if broken:
                raise ValueError(f"proposal has unknown evidence claim IDs: {sorted(broken)}")
            unsupported = [
                claim_id
                for claim_id in proposal.evidence_claim_ids
                if not claims[claim_id].material or not claims[claim_id].source_ids
            ]
            if unsupported:
                raise ValueError(f"proposal cites unsupported evidence claims: {unsupported}")
        return self


class TradeProposal(ProposedTrade):
    proposal_id: UUID = Field(default_factory=uuid4)
    account_name: str = Field(min_length=1, max_length=100)
    created_at: datetime
    research: ResearchBrief
    market_observation: MarketObservation

    @field_validator("account_name", mode="before")
    @classmethod
    def normalize_account(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "TradeProposal":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.research.as_of > self.created_at:
            raise ValueError("research cannot be from after proposal creation")
        if any(source.retrieved_at > self.created_at for source in self.research.sources):
            raise ValueError("research source cannot be retrieved after proposal creation")
        if self.market_observation.retrieved_at > self.created_at:
            raise ValueError("market observation cannot be retrieved after proposal creation")
        return self


class RiskRuleResult(StrictModel):
    rule: str = Field(min_length=1, max_length=100)
    passed: bool
    reason: str = Field(min_length=1, max_length=500)


class RiskDecision(StrictModel):
    decision_id: UUID
    proposal_id: UUID
    account_name: str
    outcome: RiskOutcome
    evaluated_at: datetime
    rules: list[RiskRuleResult] = Field(min_length=1)
    human_approved_at: datetime | None = None

    @model_validator(mode="after")
    def outcome_matches_rules(self) -> "RiskDecision":
        if self.outcome != RiskOutcome.REJECTED and any(not rule.passed for rule in self.rules):
            raise ValueError("only rejected decisions may contain failed rules")
        return self


class PaperOrder(StrictModel):
    order_id: UUID
    decision_id: UUID
    proposal_id: UUID
    account_name: str
    symbol: str
    side: OrderSide
    quantity: Annotated[int, Field(strict=True, gt=0)]
    observation: MarketObservation
    submitted_at: datetime


class ExecutionResult(StrictModel):
    execution_id: UUID
    order_id: UUID
    status: ExecutionStatus
    executed_at: datetime
    quantity: int = Field(ge=0)
    execution_price: Decimal | None = Field(default=None, gt=0)
    cash_after: Decimal | None = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=500)
