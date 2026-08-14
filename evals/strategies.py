"""Deterministic baselines and offline architecture proxies for replay."""

import hashlib
import random
from decimal import Decimal
from typing import Protocol

from .models import DecisionFixture, StrategyDecision


class Strategy(Protocol):
    name: str

    def decide(self, fixture: DecisionFixture, seed: int) -> StrategyDecision: ...


def _invest(symbols: list[str]) -> dict[str, Decimal]:
    if not symbols:
        return {}
    weight = Decimal("1") / Decimal(len(symbols))
    return {symbol: weight for symbol in symbols}


class NoTrade:
    name = "no_trade"

    def decide(self, fixture, seed):
        return StrategyDecision()


class BuyAndHold:
    name = "buy_and_hold"

    def decide(self, fixture, seed):
        return StrategyDecision(weights={fixture.benchmark_symbol: Decimal("1")})


class EqualWeight:
    name = "equal_weight"

    def decide(self, fixture, seed):
        return StrategyDecision(weights=_invest(sorted(fixture.prices)))


class RandomValidTrades:
    name = "random_valid_trades"

    def decide(self, fixture, seed):
        digest = hashlib.sha256(f"{seed}:{fixture.scenario_id}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        symbols = sorted(fixture.prices)
        chosen = rng.sample(symbols, rng.randint(0, len(symbols)))
        return StrategyDecision(weights=_invest(chosen))


class Momentum:
    name = "momentum"

    def decide(self, fixture, seed):
        symbol, score = max(fixture.trailing_returns.items(), key=lambda item: (item[1], item[0]))
        return StrategyDecision(weights={symbol: Decimal("1")} if score > 0 else {})


class SingleAgent:
    name = "single_agent"

    def decide(self, fixture, seed):
        sentiment = sum((source.sentiment for source in fixture.sources), Decimal("0"))
        ranked = max(
            fixture.trailing_returns,
            key=lambda symbol: (fixture.trailing_returns[symbol], symbol),
        )
        weights = {ranked: Decimal("1")} if sentiment >= 0 else {}
        return StrategyDecision(weights=weights, latency_ms=120, model_cost_usd=Decimal("0.0010"))


class MultiAgent:
    name = "multi_agent"

    def decide(self, fixture, seed):
        sentiment = sum((source.sentiment for source in fixture.sources), Decimal("0"))
        ranked = sorted(
            fixture.trailing_returns,
            key=lambda symbol: (fixture.trailing_returns[symbol], symbol),
            reverse=True,
        )
        chosen = [symbol for symbol in ranked[:2] if fixture.trailing_returns[symbol] > 0]
        weights = _invest(chosen) if sentiment >= Decimal("-0.25") else {}
        return StrategyDecision(weights=weights, latency_ms=260, model_cost_usd=Decimal("0.0025"))


def default_strategies() -> list[Strategy]:
    return [
        BuyAndHold(),
        EqualWeight(),
        NoTrade(),
        RandomValidTrades(),
        Momentum(),
        SingleAgent(),
        MultiAgent(),
    ]
