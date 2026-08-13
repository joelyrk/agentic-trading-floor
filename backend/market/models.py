"""Market-data contracts shared at provider, MCP, persistence, and API boundaries."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,14}$"


def normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(SYMBOL_PATTERN, normalized):
        raise ValueError("symbol must be a valid 1-15 character ticker")
    return normalized


class ObservationSource(StrEnum):
    MASSIVE = "massive"
    SIMULATOR = "simulator"


class DataMode(StrEnum):
    END_OF_DAY = "end_of_day"
    DELAYED = "delayed"
    REAL_TIME = "real_time"
    SIMULATED = "simulated"


class FallbackPolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    EXPLICIT_SIMULATOR = "explicit_simulator"
    LAST_KNOWN_GOOD = "last_known_good"


class MarketObservation(BaseModel):
    """One immutable, point-in-time price observation."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=SYMBOL_PATTERN)
    price: Decimal = Field(gt=0, max_digits=20, decimal_places=8)
    currency: str = Field(min_length=3, max_length=3)
    market_timestamp: datetime
    retrieved_at: datetime
    source: ObservationSource
    mode: DataMode
    is_stale: bool
    provider_endpoint: str = Field(min_length=1, max_length=240)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("market_timestamp", "retrieved_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def enforce_point_in_time(self) -> "MarketObservation":
        if self.market_timestamp > self.retrieved_at:
            raise ValueError("market_timestamp cannot be after retrieved_at")
        return self


class MarketStatus(BaseModel):
    provider: ObservationSource
    mode: DataMode
    configured_provider: ObservationSource
    configured_mode: DataMode
    fallback_policy: FallbackPolicy
    is_market_open: bool
    last_successful_observation: MarketObservation | None
    freshness_threshold_seconds: int = Field(gt=0)
    degraded: bool
    error_summary: str | None
