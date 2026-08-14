from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.market.config import MarketSettings
from backend.market.models import (
    DataMode,
    FallbackPolicy,
    MarketObservation,
    ObservationSource,
)
from backend.market.provider import EntitlementError
from backend.market.service import MarketDataError, MarketService


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class StubProvider:
    def __init__(self, source, mode, clock, error=None):
        self.source = source
        self.mode = mode
        self.clock = clock
        self.error = error

    def observe(self, symbol: str) -> MarketObservation:
        if self.error:
            raise self.error
        return MarketObservation(
            symbol=symbol,
            price=Decimal("100"),
            currency="USD",
            market_timestamp=self.clock.now(),
            retrieved_at=self.clock.now(),
            source=self.source,
            mode=self.mode,
            is_stale=False,
            provider_endpoint="stub",
        )

    def is_market_open(self) -> bool:
        return True


def settings(policy=FallbackPolicy.FAIL_CLOSED, threshold=60) -> MarketSettings:
    return MarketSettings(
        mode=DataMode.END_OF_DAY,
        fallback_policy=policy,
        massive_api_key="key",
        freshness_threshold_seconds=threshold,
        request_timeout_seconds=5,
    )


def test_freshness_boundary_with_injected_clock() -> None:
    start = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    clock = MutableClock(start)
    provider = StubProvider(ObservationSource.MASSIVE, DataMode.END_OF_DAY, clock)
    service = MarketService(settings(threshold=60), provider, clock)

    original = service.observe("AAPL")
    clock.value = start + timedelta(seconds=60)
    assert service._with_freshness(original).is_stale is False
    clock.value += timedelta(microseconds=1)
    assert service._with_freshness(original).is_stale is True


def test_massive_failure_is_fail_closed_without_silent_simulator() -> None:
    clock = MutableClock(datetime(2026, 8, 13, 12, tzinfo=timezone.utc))
    massive = StubProvider(
        ObservationSource.MASSIVE,
        DataMode.END_OF_DAY,
        clock,
        EntitlementError("denied"),
    )
    simulator = StubProvider(ObservationSource.SIMULATOR, DataMode.SIMULATED, clock)
    service = MarketService(settings(), massive, clock, simulator)

    with pytest.raises(MarketDataError, match="entitlement_failure"):
        service.observe("AAPL")
    assert service.status().degraded is True
    assert service.status().last_successful_observation is None


def test_invalid_symbol_is_rejected_before_provider_call() -> None:
    clock = MutableClock(datetime(2026, 8, 13, 12, tzinfo=timezone.utc))
    provider = StubProvider(ObservationSource.MASSIVE, DataMode.END_OF_DAY, clock)
    service = MarketService(settings(), provider, clock)

    with pytest.raises(MarketDataError, match="invalid_symbol"):
        service.observe("AAPL; DROP TABLE")


def test_explicit_simulator_fallback_is_visible_in_status_and_observation() -> None:
    clock = MutableClock(datetime(2026, 8, 13, 12, tzinfo=timezone.utc))
    massive = StubProvider(
        ObservationSource.MASSIVE,
        DataMode.END_OF_DAY,
        clock,
        EntitlementError("denied"),
    )
    simulator = StubProvider(ObservationSource.SIMULATOR, DataMode.SIMULATED, clock)
    service = MarketService(settings(FallbackPolicy.EXPLICIT_SIMULATOR), massive, clock, simulator)

    result = service.observe("AAPL")
    status = service.status()
    assert result.source == ObservationSource.SIMULATOR
    assert result.mode == DataMode.SIMULATED
    assert status.provider == ObservationSource.SIMULATOR
    assert status.configured_provider == ObservationSource.MASSIVE
    assert status.degraded is True
    assert "entitlement_failure" in (status.error_summary or "")


def test_last_known_good_is_returned_and_rechecked_for_freshness() -> None:
    start = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    clock = MutableClock(start)
    provider = StubProvider(ObservationSource.MASSIVE, DataMode.END_OF_DAY, clock)
    service = MarketService(settings(FallbackPolicy.LAST_KNOWN_GOOD, threshold=60), provider, clock)
    original = service.observe("AAPL")
    provider.error = EntitlementError("denied")
    clock.value = start + timedelta(seconds=61)

    cached = service.observe("AAPL")
    assert cached.price == original.price
    assert cached.is_stale is True
    assert service.status().degraded is True
