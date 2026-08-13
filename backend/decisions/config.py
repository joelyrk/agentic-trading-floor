"""Configuration for deterministic risk policy."""

import json
import os
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from backend.market.models import DataMode, normalize_symbol


class RiskPolicy(BaseModel):
    max_position_percentage: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    max_symbol_concentration: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    max_sector_concentration: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    minimum_cash_reserve: Decimal = Field(default=Decimal("500"), ge=0)
    maximum_order_notional: Decimal = Field(default=Decimal("2500"), gt=0)
    maximum_daily_turnover: Decimal = Field(default=Decimal("5000"), gt=0)
    maximum_drawdown: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    allowed_universe: frozenset[str] = Field(default_factory=frozenset)
    allowed_market_modes: frozenset[DataMode] = Field(
        default_factory=lambda: frozenset({DataMode.END_OF_DAY, DataMode.SIMULATED})
    )
    sector_by_symbol: dict[str, str] = Field(default_factory=dict)
    human_approval_enabled: bool = False
    human_approval_notional: Decimal = Field(default=Decimal("2000"), gt=0)
    automated_replay: bool = False

    @field_validator("allowed_universe", mode="before")
    @classmethod
    def normalize_universe(cls, value):
        return frozenset(normalize_symbol(item) for item in value)

    @field_validator("sector_by_symbol", mode="before")
    @classmethod
    def normalize_sector_map(cls, value):
        return {normalize_symbol(k): str(v).strip().lower() for k, v in value.items()}

    @classmethod
    def from_env(cls) -> "RiskPolicy":
        universe = [s for s in os.getenv("RISK_ALLOWED_UNIVERSE", "").split(",") if s.strip()]
        sector_map = json.loads(os.getenv("RISK_SECTOR_MAP", "{}"))
        modes = [
            DataMode(item.strip())
            for item in os.getenv("RISK_ALLOWED_MARKET_MODES", "end_of_day,simulated").split(",")
            if item.strip()
        ]
        truthy = {"1", "true", "yes", "on"}
        return cls(
            max_position_percentage=os.getenv("RISK_MAX_POSITION_PERCENTAGE", "0.30"),
            max_symbol_concentration=os.getenv("RISK_MAX_SYMBOL_CONCENTRATION", "0.30"),
            max_sector_concentration=os.getenv("RISK_MAX_SECTOR_CONCENTRATION", "0.50"),
            minimum_cash_reserve=os.getenv("RISK_MINIMUM_CASH_RESERVE", "500"),
            maximum_order_notional=os.getenv("RISK_MAXIMUM_ORDER_NOTIONAL", "2500"),
            maximum_daily_turnover=os.getenv("RISK_MAXIMUM_DAILY_TURNOVER", "5000"),
            maximum_drawdown=os.getenv("RISK_MAXIMUM_DRAWDOWN", "0.25"),
            allowed_universe=frozenset(universe),
            allowed_market_modes=frozenset(modes),
            sector_by_symbol=sector_map,
            human_approval_enabled=os.getenv("RISK_HUMAN_APPROVAL_ENABLED", "false").lower() in truthy,
            human_approval_notional=os.getenv("RISK_HUMAN_APPROVAL_NOTIONAL", "2000"),
            automated_replay=os.getenv("AUTOMATED_REPLAY", "false").lower() in truthy,
        )
