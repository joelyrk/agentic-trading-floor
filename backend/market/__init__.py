"""Typed market-data facade used by accounts, the API, and the MCP server."""

from .config import MarketSettings, get_market_settings
from .models import (
    DataMode,
    FallbackPolicy,
    MarketObservation,
    MarketStatus,
    ObservationSource,
)
from .service import (
    MarketDataError,
    MarketService,
    build_market_service,
    get_market_service,
)


def get_market_observation(symbol: str) -> MarketObservation:
    """Return a fully attributed observation from the configured service."""
    return get_market_service().observe(symbol)


def get_share_price(symbol: str) -> float:
    """Compatibility helper for legacy callers; prefer ``get_market_observation``."""
    return float(get_market_observation(symbol).price)


def is_market_open() -> bool:
    """Return the configured service's best-known market-open state."""
    return get_market_service().is_market_open()


__all__ = [
    "DataMode",
    "FallbackPolicy",
    "MarketDataError",
    "MarketObservation",
    "MarketService",
    "MarketSettings",
    "MarketStatus",
    "ObservationSource",
    "build_market_service",
    "get_market_observation",
    "get_market_service",
    "get_market_settings",
    "get_share_price",
    "is_market_open",
]
