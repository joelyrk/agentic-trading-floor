"""Validated environment configuration for market data."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from .models import DataMode, FallbackPolicy

load_dotenv()


class MarketSettings(BaseModel):
    mode: DataMode
    fallback_policy: FallbackPolicy
    massive_api_key: str | None = None
    freshness_threshold_seconds: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0, le=120)
    cache_ttl_seconds: int = Field(default=0, ge=0, le=86_400)

    @model_validator(mode="after")
    def validate_capabilities(self) -> "MarketSettings":
        if self.mode == DataMode.END_OF_DAY and not self.massive_api_key:
            raise ValueError("MARKET_DATA_MODE=end_of_day requires MASSIVE_API_KEY")
        if self.mode in {DataMode.DELAYED, DataMode.REAL_TIME}:
            raise ValueError(
                f"MARKET_DATA_MODE={self.mode.value} is unavailable: this build supports "
                "Massive end_of_day and simulated modes only"
            )
        if self.mode == DataMode.SIMULATED and self.fallback_policy != FallbackPolicy.FAIL_CLOSED:
            raise ValueError("fallback policies only apply when MARKET_DATA_MODE=end_of_day")
        return self

    @classmethod
    def from_env(cls) -> "MarketSettings":
        key = os.getenv("MASSIVE_API_KEY", "").strip() or None
        mode_text = os.getenv("MARKET_DATA_MODE", "").strip().lower()
        mode = (
            DataMode(mode_text)
            if mode_text
            else (DataMode.END_OF_DAY if key else DataMode.SIMULATED)
        )
        fallback_text = os.getenv("MARKET_DATA_FALLBACK", "").strip().lower()
        fallback = FallbackPolicy(fallback_text or FallbackPolicy.FAIL_CLOSED.value)
        default_freshness = 345_600 if mode == DataMode.END_OF_DAY else 300
        default_cache_ttl = 3_600 if mode == DataMode.END_OF_DAY else 0
        return cls(
            mode=mode,
            fallback_policy=fallback,
            massive_api_key=key,
            freshness_threshold_seconds=int(
                os.getenv("MARKET_DATA_FRESHNESS_SECONDS", str(default_freshness))
            ),
            request_timeout_seconds=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "10")),
            cache_ttl_seconds=int(
                os.getenv("MARKET_DATA_CACHE_SECONDS", str(default_cache_ttl))
            ),
        )


@lru_cache(maxsize=1)
def get_market_settings() -> MarketSettings:
    return MarketSettings.from_env()
