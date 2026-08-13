"""Intentional Massive previous-close adapter for end-of-day entitlements."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from massive import RESTClient

from .clock import Clock
from .models import DataMode, MarketObservation, ObservationSource
from .provider import (
    AuthenticationError,
    EmptyMarketDayError,
    EntitlementError,
    MalformedResponseError,
    MarketProviderError,
    ProviderTimeoutError,
)

PREVIOUS_CLOSE_ENDPOINT = "/v2/aggs/ticker/{symbol}/prev"


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status", None)


def _normalize_error(exc: Exception) -> MarketProviderError:
    status = _status_code(exc)
    message = str(exc).lower()
    if status == 401 or "unauthorized" in message or "api key" in message:
        return AuthenticationError("Massive authentication failed")
    if status == 403 or "forbidden" in message or "entitlement" in message:
        return EntitlementError("Massive end-of-day entitlement is unavailable")
    if isinstance(exc, TimeoutError) or "timed out" in message or "timeout" in message:
        return ProviderTimeoutError("Massive request timed out")
    return MarketProviderError("Massive request failed")


def _value(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


class MassiveEodProvider:
    """Fetch only Massive's supported previous-close aggregate endpoint."""

    source = ObservationSource.MASSIVE
    mode = DataMode.END_OF_DAY

    def __init__(
        self,
        api_key: str,
        clock: Clock,
        timeout_seconds: float = 10,
        client_factory: Callable[..., Any] = RESTClient,
    ):
        self.clock = clock
        self._client = client_factory(
            api_key,
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
        )

    def observe(self, symbol: str) -> MarketObservation:
        normalized = symbol.strip().upper()
        try:
            rows = self._client.get_previous_close_agg(normalized)
        except Exception as exc:
            raise _normalize_error(exc) from exc
        if not rows:
            raise EmptyMarketDayError(f"Massive returned no previous close for {normalized}")

        row = rows[0]
        raw_price = _value(row, "close", "c")
        raw_timestamp = _value(row, "timestamp", "t")
        try:
            price = Decimal(str(raw_price))
            if not price.is_finite() or price <= 0:
                raise ValueError
            timestamp_number = float(raw_timestamp)
            # Aggregate timestamps are Unix milliseconds; tolerate seconds in test doubles.
            if timestamp_number > 10_000_000_000:
                timestamp_number /= 1000
            market_timestamp = datetime.fromtimestamp(timestamp_number, tz=timezone.utc)
        except (InvalidOperation, TypeError, ValueError, OSError, OverflowError) as exc:
            raise MalformedResponseError(
                f"Massive returned a malformed previous-close payload for {normalized}"
            ) from exc

        return MarketObservation(
            symbol=normalized,
            price=price,
            currency="USD",
            market_timestamp=market_timestamp,
            retrieved_at=self.clock.now(),
            source=self.source,
            mode=self.mode,
            is_stale=False,
            provider_endpoint=PREVIOUS_CLOSE_ENDPOINT.format(symbol=normalized),
        )

    def is_market_open(self) -> bool:
        # The EOD product does not need or probe a live market-status capability.
        now = self.clock.now()
        return now.weekday() < 5
