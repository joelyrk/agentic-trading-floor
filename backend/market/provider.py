"""Provider interface and normalized market-data failures."""

from typing import Protocol

from .models import DataMode, MarketObservation, ObservationSource


class MarketProviderError(RuntimeError):
    code = "provider_error"


class AuthenticationError(MarketProviderError):
    code = "authentication_failure"


class EntitlementError(MarketProviderError):
    code = "entitlement_failure"


class ProviderTimeoutError(MarketProviderError):
    code = "timeout"


class MalformedResponseError(MarketProviderError):
    code = "malformed_payload"


class EmptyMarketDayError(MarketProviderError):
    code = "empty_market_day"


class MarketProvider(Protocol):
    source: ObservationSource
    mode: DataMode

    def observe(self, symbol: str) -> MarketObservation: ...

    def is_market_open(self) -> bool: ...
