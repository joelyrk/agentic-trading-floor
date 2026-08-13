"""Deterministic simulator adapter. Its synthetic nature is always explicit."""

from decimal import Decimal

from backend.market_simulator import simulated_price

from .clock import Clock
from .models import DataMode, MarketObservation, ObservationSource


class SimulatorProvider:
    source = ObservationSource.SIMULATOR
    mode = DataMode.SIMULATED

    def __init__(self, clock: Clock):
        self.clock = clock

    def observe(self, symbol: str) -> MarketObservation:
        now = self.clock.now()
        return MarketObservation(
            symbol=symbol,
            price=Decimal(str(simulated_price(symbol, now))),
            currency="USD",
            market_timestamp=now,
            retrieved_at=now,
            source=self.source,
            mode=self.mode,
            is_stale=False,
            provider_endpoint="deterministic-simulator/v1",
        )

    def is_market_open(self) -> bool:
        return True
