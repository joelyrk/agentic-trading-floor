"""Pure deterministic risk evaluation."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .config import RiskPolicy
from .models import OrderSide, RiskDecision, RiskOutcome, RiskRuleResult, TradeProposal

EXECUTION_SPREAD = Decimal("0.002")


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: Decimal
    holdings: dict[str, int]
    prices: dict[str, Decimal]
    sectors: dict[str, str]
    daily_turnover: Decimal = Decimal("0")
    peak_value: Decimal | None = None

    @property
    def value(self) -> Decimal:
        return self.cash + sum(
            self.prices.get(symbol, Decimal("0")) * quantity
            for symbol, quantity in self.holdings.items()
        )


class RiskEngine:
    def __init__(self, policy: RiskPolicy):
        self.policy = policy

    @staticmethod
    def _result(rule: str, passed: bool, reason: str) -> RiskRuleResult:
        return RiskRuleResult(rule=rule, passed=passed, reason=reason)

    def evaluate(self, proposal: TradeProposal, portfolio: PortfolioSnapshot, now) -> RiskDecision:
        p = self.policy
        observation = proposal.market_observation
        price = observation.price
        risk_price = (
            price * (Decimal("1") + EXECUTION_SPREAD)
            if proposal.side == OrderSide.BUY
            else price * (Decimal("1") - EXECUTION_SPREAD)
        )
        notional = risk_price * proposal.quantity
        current_qty = portfolio.holdings.get(proposal.symbol, 0)
        resulting_qty = (
            current_qty + proposal.quantity
            if proposal.side == OrderSide.BUY
            else current_qty - proposal.quantity
        )
        resulting_cash = (
            portfolio.cash - notional
            if proposal.side == OrderSide.BUY
            else portfolio.cash + notional
        )
        resulting_value = portfolio.value
        position_value = max(Decimal("0"), price * resulting_qty)
        concentration = position_value / resulting_value if resulting_value > 0 else Decimal("1")
        rules = [
            self._result(
                "allowed_universe",
                not p.allowed_universe or proposal.symbol in p.allowed_universe,
                "symbol is allowed"
                if not p.allowed_universe or proposal.symbol in p.allowed_universe
                else "symbol is outside the allowed universe",
            ),
            self._result(
                "positive_integral_quantity",
                isinstance(proposal.quantity, int) and proposal.quantity > 0,
                "quantity is a positive integer",
            ),
            self._result(
                "market_data_freshness",
                not observation.is_stale,
                "market observation is fresh"
                if not observation.is_stale
                else "market observation is stale",
            ),
            self._result(
                "market_data_mode",
                observation.mode in p.allowed_market_modes,
                f"market mode {observation.mode.value} is allowed"
                if observation.mode in p.allowed_market_modes
                else f"market mode {observation.mode.value} is incompatible",
            ),
            self._result(
                "maximum_order_notional",
                notional <= p.maximum_order_notional,
                f"order notional {notional} is within limit {p.maximum_order_notional}",
            ),
            self._result(
                "maximum_daily_turnover",
                portfolio.daily_turnover + notional <= p.maximum_daily_turnover,
                f"resulting daily turnover {portfolio.daily_turnover + notional} is within limit {p.maximum_daily_turnover}",
            ),
            self._result(
                "sufficient_holdings",
                proposal.side == OrderSide.BUY or resulting_qty >= 0,
                "sell quantity does not exceed holdings"
                if resulting_qty >= 0
                else "sell quantity exceeds holdings",
            ),
            self._result(
                "minimum_cash_reserve",
                proposal.side == OrderSide.SELL or resulting_cash >= p.minimum_cash_reserve,
                f"resulting cash {resulting_cash} meets reserve {p.minimum_cash_reserve}",
            ),
        ]

        concentration_applies = proposal.side == OrderSide.BUY
        rules.extend(
            [
                self._result(
                    "maximum_position_percentage",
                    not concentration_applies or concentration <= p.max_position_percentage,
                    f"resulting position percentage {concentration:.4f} is within limit {p.max_position_percentage}",
                ),
                self._result(
                    "maximum_symbol_concentration",
                    not concentration_applies or concentration <= p.max_symbol_concentration,
                    f"resulting symbol concentration {concentration:.4f} is within limit {p.max_symbol_concentration}",
                ),
            ]
        )

        # The model's sector label is explanatory only. Policy classification is
        # deterministic; unmapped symbols share a conservative bucket.
        sector = p.sector_by_symbol.get(proposal.symbol, "unclassified")
        sector_value = (
            sum(
                portfolio.prices.get(symbol, Decimal("0")) * quantity
                for symbol, quantity in portfolio.holdings.items()
                if portfolio.sectors.get(symbol, "unclassified") == sector
                and symbol != proposal.symbol
            )
            + position_value
        )
        sector_concentration = (
            sector_value / resulting_value if resulting_value > 0 else Decimal("1")
        )
        rules.append(
            self._result(
                "maximum_sector_concentration",
                not concentration_applies or sector_concentration <= p.max_sector_concentration,
                f"resulting sector concentration {sector_concentration:.4f} is within limit {p.max_sector_concentration}",
            )
        )
        peak = portfolio.peak_value or portfolio.value
        drawdown = (
            (peak - portfolio.value) / peak if peak > 0 and portfolio.value < peak else Decimal("0")
        )
        rules.append(
            self._result(
                "maximum_drawdown_kill_switch",
                drawdown < p.maximum_drawdown,
                f"drawdown {drawdown:.4f} is below kill-switch threshold {p.maximum_drawdown}",
            )
        )

        rejected = any(not rule.passed for rule in rules)
        needs_human = (
            not rejected
            and p.human_approval_enabled
            and not p.automated_replay
            and notional >= p.human_approval_notional
        )
        outcome = (
            RiskOutcome.REJECTED
            if rejected
            else (RiskOutcome.PENDING_HUMAN if needs_human else RiskOutcome.APPROVED)
        )
        decision_id = uuid5(NAMESPACE_URL, f"risk:{proposal.proposal_id}")
        return RiskDecision(
            decision_id=decision_id,
            proposal_id=proposal.proposal_id,
            account_name=proposal.account_name,
            outcome=outcome,
            evaluated_at=now,
            rules=rules,
        )
