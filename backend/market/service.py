"""Freshness, fallback, cache, and status policy over market providers."""

from datetime import timedelta
from functools import lru_cache

from pydantic import ValidationError

from .clock import Clock, UtcClock
from .config import MarketSettings, get_market_settings
from .massive import MassiveEodProvider
from .models import DataMode, FallbackPolicy, MarketObservation, MarketStatus, normalize_symbol
from .provider import MarketProvider, MarketProviderError
from .simulator import SimulatorProvider


class MarketDataError(RuntimeError):
    """A safe cycle-stopping market-data error."""


class MarketService:
    def __init__(
        self,
        settings: MarketSettings,
        provider: MarketProvider,
        clock: Clock,
        simulator: MarketProvider | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.clock = clock
        self.simulator = simulator
        self._last_good_by_symbol: dict[str, MarketObservation] = {}
        self._last_success: MarketObservation | None = None
        self._degraded = False
        self._error_summary: str | None = None

    def _with_freshness(self, observation: MarketObservation) -> MarketObservation:
        age = self.clock.now() - observation.market_timestamp
        threshold = timedelta(seconds=self.settings.freshness_threshold_seconds)
        return observation.model_copy(update={"is_stale": age > threshold})

    def _success(self, observation: MarketObservation, degraded: bool = False) -> MarketObservation:
        observation = self._with_freshness(observation)
        self._last_good_by_symbol[observation.symbol] = observation
        self._last_success = observation
        self._degraded = degraded
        if not degraded:
            self._error_summary = None
        return observation

    def observe(self, symbol: str) -> MarketObservation:
        try:
            normalized = normalize_symbol(symbol)
        except (AttributeError, ValueError) as exc:
            raise MarketDataError(f"invalid_symbol: {exc}") from exc
        try:
            return self._success(self.provider.observe(normalized))
        except (MarketProviderError, ValidationError) as exc:
            self._degraded = True
            code = getattr(exc, "code", "invalid_observation")
            self._error_summary = f"{code}: {exc}"

            if self.settings.fallback_policy == FallbackPolicy.EXPLICIT_SIMULATOR:
                if self.simulator is None:
                    raise MarketDataError(self._error_summary) from exc
                return self._success(self.simulator.observe(normalized), degraded=True)

            if self.settings.fallback_policy == FallbackPolicy.LAST_KNOWN_GOOD:
                cached = self._last_good_by_symbol.get(normalized)
                if cached is not None:
                    return self._with_freshness(cached)

            raise MarketDataError(self._error_summary) from exc

    def is_market_open(self) -> bool:
        return self.provider.is_market_open()

    def status(self) -> MarketStatus:
        effective = self._last_success
        return MarketStatus(
            provider=effective.source if effective else self.provider.source,
            mode=effective.mode if effective else self.provider.mode,
            configured_provider=self.provider.source,
            configured_mode=self.provider.mode,
            fallback_policy=self.settings.fallback_policy,
            is_market_open=self.is_market_open(),
            last_successful_observation=self._last_success,
            freshness_threshold_seconds=self.settings.freshness_threshold_seconds,
            degraded=self._degraded,
            error_summary=self._error_summary,
        )


def build_market_service(
    settings: MarketSettings | None = None,
    clock: Clock | None = None,
    provider: MarketProvider | None = None,
) -> MarketService:
    settings = settings or get_market_settings()
    clock = clock or UtcClock()
    simulator = SimulatorProvider(clock)
    if provider is None:
        if settings.mode == DataMode.SIMULATED:
            provider = simulator
        else:
            provider = MassiveEodProvider(
                settings.massive_api_key or "",
                clock,
                timeout_seconds=settings.request_timeout_seconds,
            )
    fallback_simulator = (
        simulator if settings.fallback_policy == FallbackPolicy.EXPLICIT_SIMULATOR else None
    )
    return MarketService(settings, provider, clock, fallback_simulator)


@lru_cache(maxsize=1)
def get_market_service() -> MarketService:
    return build_market_service()
