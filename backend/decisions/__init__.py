"""Structured proposal, deterministic risk, and paper-execution domain."""

from backend.research import EvidenceClaim, EvidenceStance, ResearchBrief, SourceRecord

from .config import RiskPolicy
from .models import (
    ExecutionResult,
    ExecutionStatus,
    OrderSide,
    PaperOrder,
    RiskDecision,
    RiskOutcome,
    RiskRuleResult,
    TradeProposal,
    TraderRecommendation,
    TradingDecision,
)
from .risk import PortfolioSnapshot, RiskEngine
from .services import DecisionPipeline, ExecutionService, ProposalService, RiskService

__all__ = [
    "DecisionPipeline",
    "ExecutionResult",
    "ExecutionService",
    "ExecutionStatus",
    "EvidenceClaim",
    "EvidenceStance",
    "OrderSide",
    "PaperOrder",
    "PortfolioSnapshot",
    "ProposalService",
    "ResearchBrief",
    "SourceRecord",
    "RiskDecision",
    "RiskEngine",
    "RiskOutcome",
    "RiskPolicy",
    "RiskRuleResult",
    "RiskService",
    "TradeProposal",
    "TraderRecommendation",
    "TradingDecision",
]
