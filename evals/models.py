"""Versioned schemas for immutable fixtures and evaluation artifacts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.market.models import normalize_symbol


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamps must be timezone-aware")
    return value


class FixtureFile(StrictModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DatasetManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")
    dataset_version: str
    description: str
    scenario_count: int = Field(ge=30, le=100)
    symbols: list[str] = Field(min_length=1)
    decision_fixtures: FixtureFile
    outcome_fixtures: FixtureFile

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value):
        return [normalize_symbol(symbol) for symbol in value]


class PointInTimeSource(StrictModel):
    source_id: str
    published_at: datetime
    retrieved_at: datetime
    sentiment: Decimal = Field(ge=-1, le=1)

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def require_aware(cls, value):
        return _aware(value)


class DecisionFixture(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")
    decision_at: datetime
    market_timestamp: datetime
    retrieved_at: datetime
    prices: dict[str, Decimal]
    trailing_returns: dict[str, Decimal]
    sources: list[PointInTimeSource]
    benchmark_symbol: str

    @field_validator("decision_at", "market_timestamp", "retrieved_at")
    @classmethod
    def require_aware(cls, value):
        return _aware(value)

    @field_validator("prices", "trailing_returns", mode="before")
    @classmethod
    def normalize_price_symbols(cls, value):
        return {normalize_symbol(k): v for k, v in value.items()}

    @field_validator("benchmark_symbol", mode="before")
    @classmethod
    def normalize_benchmark(cls, value):
        return normalize_symbol(value)

    @model_validator(mode="after")
    def enforce_cutoff(self):
        if self.market_timestamp > self.retrieved_at or self.retrieved_at > self.decision_at:
            raise ValueError("market data must exist by the decision cutoff")
        for source in self.sources:
            if source.published_at > source.retrieved_at or source.retrieved_at > self.decision_at:
                raise ValueError(f"source {source.source_id} violates the decision cutoff")
        if set(self.prices) != set(self.trailing_returns):
            raise ValueError("prices and trailing returns must cover the same symbols")
        if self.benchmark_symbol not in self.prices:
            raise ValueError("benchmark symbol is missing from decision prices")
        return self


class OutcomeFixture(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str
    outcome_at: datetime
    prices: dict[str, Decimal]

    @field_validator("outcome_at")
    @classmethod
    def require_aware(cls, value):
        return _aware(value)

    @field_validator("prices", mode="before")
    @classmethod
    def normalize_symbols(cls, value):
        return {normalize_symbol(k): v for k, v in value.items()}


class StrategyDecision(StrictModel):
    weights: dict[str, Decimal] = Field(default_factory=dict)
    decision_valid: bool = True
    citation_valid: bool = True
    tool_success: bool = True
    latency_ms: int = Field(default=0, ge=0)
    model_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def valid_weights(self):
        if any(weight < 0 or weight > 1 for weight in self.weights.values()):
            raise ValueError("weights must be between zero and one")
        if sum(self.weights.values(), Decimal("0")) > Decimal("1.00000001"):
            raise ValueError("weights cannot exceed 100%")
        return self


class ReplayTiming(StrictModel):
    research_cutoff: datetime
    market_timestamp: datetime
    market_retrieved_at: datetime
    decided_at: datetime
    executed_at: datetime

    @field_validator(
        "research_cutoff",
        "market_timestamp",
        "market_retrieved_at",
        "decided_at",
        "executed_at",
    )
    @classmethod
    def require_aware(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def enforce_replay_boundary(self):
        if not (
            self.market_timestamp <= self.market_retrieved_at <= self.research_cutoff
            and self.decided_at == self.research_cutoff
            and self.executed_at == self.decided_at
        ):
            raise ValueError(
                "research, market, decision, and execution must share the replay cutoff"
            )
        return self


class ScenarioMetrics(StrictModel):
    scenario_id: str
    strategy: str
    portfolio_return: Decimal
    benchmark_return: Decimal
    turnover: Decimal
    won: bool
    decision_valid: bool
    citation_valid: bool
    tool_success: bool
    latency_ms: int
    model_cost_usd: Decimal
    order_ids: list[str] = Field(default_factory=list)
    timing: ReplayTiming | None = None


class AggregateMetrics(StrictModel):
    total_return: Decimal
    benchmark_return: Decimal
    benchmark_relative_return: Decimal
    annualized_volatility: Decimal
    sharpe: Decimal | None
    max_drawdown: Decimal
    turnover: Decimal
    win_rate: Decimal
    decision_validity: Decimal
    citation_validity: Decimal
    tool_success_rate: Decimal
    average_latency_ms: Decimal
    model_api_cost_usd: Decimal


class StrategyResult(StrictModel):
    strategy: str
    metrics: AggregateMetrics
    scenarios: list[ScenarioMetrics]


class RunMetadata(StrictModel):
    run_id: str
    dataset_id: str
    dataset_version: str
    git_sha: str
    model: str
    prompt_version: str
    configuration: dict[str, str | int | float | bool]
    seed: int
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware(cls, value):
        return _aware(value)


class EvaluationReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    metadata: RunMetadata
    results: list[StrategyResult]
    ablation: dict[str, Decimal]
    leakage_checks_passed: bool
