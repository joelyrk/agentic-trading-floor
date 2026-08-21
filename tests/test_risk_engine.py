from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.decisions import (
    EvidenceClaim,
    OrderSide,
    PortfolioSnapshot,
    ResearchBrief,
    RiskEngine,
    RiskOutcome,
    RiskPolicy,
    SourceRecord,
    TradeProposal,
)
from backend.market.models import DataMode, MarketObservation, ObservationSource

NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def observation(*, stale=False, mode=DataMode.SIMULATED, price="100") -> MarketObservation:
    return MarketObservation(
        symbol="AAPL",
        price=Decimal(price),
        currency="USD",
        market_timestamp=NOW,
        retrieved_at=NOW,
        source=ObservationSource.SIMULATOR,
        mode=mode,
        is_stale=stale,
        provider_endpoint="test/v1",
    )


def proposal(**updates) -> TradeProposal:
    source = SourceRecord(
        source_id="source-1",
        canonical_url="https://example.com/aapl",
        publisher="Example News",
        title="Apple update",
        published_at=NOW,
        retrieved_at=NOW,
        supporting_excerpt="A concise supporting excerpt.",
    )
    claim = EvidenceClaim(
        claim_id="claim-1",
        claim="A supported material claim",
        source_ids=["source-1"],
        stance="supports",
        confidence=Decimal("0.75"),
    )
    data = dict(
        account_name="risk",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=1,
        sector="technology",
        rationale="bounded test",
        created_at=NOW,
        evidence_claim_ids=["claim-1"],
        research=ResearchBrief(
            summary="test evidence", as_of=NOW, sources=[source], claims=[claim]
        ),
        market_observation=observation(),
    )
    data.update(updates)
    return TradeProposal(**data)


def snapshot(**updates) -> PortfolioSnapshot:
    data = dict(
        cash=Decimal("10000"),
        holdings={},
        prices={"AAPL": Decimal("100")},
        sectors={"AAPL": "technology"},
        daily_turnover=Decimal("0"),
        peak_value=Decimal("10000"),
    )
    data.update(updates)
    return PortfolioSnapshot(**data)


def generous_policy(**updates) -> RiskPolicy:
    data = dict(
        max_position_percentage=Decimal("1"),
        max_symbol_concentration=Decimal("1"),
        max_sector_concentration=Decimal("1"),
        minimum_cash_reserve=Decimal("0"),
        maximum_order_notional=Decimal("100000"),
        maximum_daily_turnover=Decimal("100000"),
        maximum_drawdown=Decimal("1"),
    )
    data.update(updates)
    return RiskPolicy(**data)


def failed_rules(decision) -> set[str]:
    return {rule.rule for rule in decision.rules if not rule.passed}


@pytest.mark.parametrize(
    ("policy", "trade", "portfolio", "rule"),
    [
        (
            generous_policy(allowed_universe=frozenset({"MSFT"})),
            proposal(),
            snapshot(),
            "allowed_universe",
        ),
        (
            generous_policy(),
            proposal(market_observation=observation(stale=True)),
            snapshot(),
            "market_data_freshness",
        ),
        (
            generous_policy(allowed_market_modes=frozenset({DataMode.END_OF_DAY})),
            proposal(),
            snapshot(),
            "market_data_mode",
        ),
        (
            generous_policy(maximum_order_notional=Decimal("100")),
            proposal(),
            snapshot(),
            "maximum_order_notional",
        ),
        (
            generous_policy(maximum_daily_turnover=Decimal("200")),
            proposal(),
            snapshot(daily_turnover=Decimal("150")),
            "maximum_daily_turnover",
        ),
        (
            generous_policy(),
            proposal(side=OrderSide.SELL, quantity=2),
            snapshot(holdings={"AAPL": 1}),
            "sufficient_holdings",
        ),
        (
            generous_policy(minimum_cash_reserve=Decimal("9950")),
            proposal(),
            snapshot(),
            "minimum_cash_reserve",
        ),
        (
            generous_policy(max_position_percentage=Decimal("0.009")),
            proposal(),
            snapshot(),
            "maximum_position_percentage",
        ),
        (
            generous_policy(max_symbol_concentration=Decimal("0.009")),
            proposal(),
            snapshot(),
            "maximum_symbol_concentration",
        ),
        (
            generous_policy(max_sector_concentration=Decimal("0.009")),
            proposal(),
            snapshot(),
            "maximum_sector_concentration",
        ),
        (
            generous_policy(maximum_drawdown=Decimal("0.20")),
            proposal(),
            snapshot(cash=Decimal("7000"), peak_value=Decimal("10000")),
            "maximum_drawdown_kill_switch",
        ),
    ],
)
def test_each_risk_rule_rejects_at_boundary(policy, trade, portfolio, rule) -> None:
    decision = RiskEngine(policy).evaluate(trade, portfolio, NOW)
    assert decision.outcome == RiskOutcome.REJECTED
    assert rule in failed_rules(decision)


def test_exact_limits_pass_and_stable_decision_id() -> None:
    policy = generous_policy(
        maximum_order_notional=Decimal("100.2"), maximum_daily_turnover=Decimal("200.2")
    )
    trade = proposal()
    portfolio = snapshot(daily_turnover=Decimal("100"))
    first = RiskEngine(policy).evaluate(trade, portfolio, NOW)
    second = RiskEngine(policy).evaluate(trade, portfolio, NOW)
    assert first.outcome == RiskOutcome.APPROVED
    assert first.decision_id == second.decision_id


def test_oversized_buy_is_deterministically_reduced_to_largest_compliant_quantity() -> None:
    trade = proposal(quantity=50)
    decision = RiskEngine(
        generous_policy(maximum_order_notional=Decimal("2500"))
    ).evaluate(trade, snapshot(), NOW)

    assert decision.outcome == RiskOutcome.APPROVED
    assert decision.requested_quantity == 50
    assert decision.approved_quantity == 24
    sizing = next(rule for rule in decision.rules if rule.rule == "deterministic_order_sizing")
    assert sizing.passed
    assert sizing.reason == (
        "requested quantity 50 reduced to 24 whole shares to satisfy maximum_order_notional"
    )
    notional = next(rule for rule in decision.rules if rule.rule == "maximum_order_notional")
    assert notional.passed
    assert "2404.800" in notional.reason
    assert "is within limit 2500" in notional.reason


def test_concentration_limit_deterministically_sizes_buy() -> None:
    decision = RiskEngine(
        generous_policy(
            max_position_percentage=Decimal("0.30"),
            max_symbol_concentration=Decimal("0.30"),
        )
    ).evaluate(proposal(quantity=50), snapshot(), NOW)

    assert decision.outcome == RiskOutcome.APPROVED
    assert decision.approved_quantity == 30


def test_unsatisfiable_limit_rejects_with_truthful_reason() -> None:
    decision = RiskEngine(
        generous_policy(maximum_order_notional=Decimal("100"))
    ).evaluate(proposal(), snapshot(), NOW)

    assert decision.outcome == RiskOutcome.REJECTED
    assert decision.approved_quantity is None
    notional = next(rule for rule in decision.rules if rule.rule == "maximum_order_notional")
    assert not notional.passed
    assert notional.reason == "order notional 100.200 exceeds limit 100"


def test_human_approval_policy_and_replay_override() -> None:
    trade = proposal()
    pending = RiskEngine(
        generous_policy(
            human_approval_enabled=True,
            human_approval_notional=Decimal("100"),
            automated_replay=False,
        )
    ).evaluate(trade, snapshot(), NOW)
    replay = RiskEngine(
        generous_policy(
            human_approval_enabled=True,
            human_approval_notional=Decimal("100"),
            automated_replay=True,
        )
    ).evaluate(trade, snapshot(), NOW)
    assert pending.outcome == RiskOutcome.PENDING_HUMAN
    assert replay.outcome == RiskOutcome.APPROVED
