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

    def _quantity_metrics(
        self, proposal: TradeProposal, portfolio: PortfolioSnapshot, quantity: int
    ) -> dict[str, Decimal | int]:
        """Calculate deterministic policy inputs for one whole-share quantity."""
        p = self.policy
        price = proposal.market_observation.price
        risk_price = (
            price * (Decimal("1") + EXECUTION_SPREAD)
            if proposal.side == OrderSide.BUY
            else price * (Decimal("1") - EXECUTION_SPREAD)
        )
        notional = risk_price * quantity
        current_qty = portfolio.holdings.get(proposal.symbol, 0)
        resulting_qty = (
            current_qty + quantity
            if proposal.side == OrderSide.BUY
            else current_qty - quantity
        )
        resulting_cash = (
            portfolio.cash - notional
            if proposal.side == OrderSide.BUY
            else portfolio.cash + notional
        )
        resulting_value = portfolio.value
        position_value = max(Decimal("0"), price * resulting_qty)
        concentration = position_value / resulting_value if resulting_value > 0 else Decimal("1")
        sector = p.sector_by_symbol.get(proposal.symbol, "unclassified")
        other_sector_value = sum(
            portfolio.prices.get(symbol, Decimal("0")) * held_quantity
            for symbol, held_quantity in portfolio.holdings.items()
            if portfolio.sectors.get(symbol, "unclassified") == sector
            and symbol != proposal.symbol
        )
        sector_value = other_sector_value + position_value
        sector_concentration = (
            sector_value / resulting_value if resulting_value > 0 else Decimal("1")
        )
        return {
            "risk_price": risk_price,
            "notional": notional,
            "resulting_qty": resulting_qty,
            "resulting_cash": resulting_cash,
            "resulting_value": resulting_value,
            "concentration": concentration,
            "sector_concentration": sector_concentration,
            "daily_turnover": portfolio.daily_turnover + notional,
        }

    def _sizing_failures(
        self, proposal: TradeProposal, portfolio: PortfolioSnapshot, quantity: int
    ) -> list[str]:
        """Return quantity-dependent limits breached by a proposed whole-share size."""
        p = self.policy
        metrics = self._quantity_metrics(proposal, portfolio, quantity)
        failures = []
        if metrics["notional"] > p.maximum_order_notional:
            failures.append("maximum_order_notional")
        if metrics["daily_turnover"] > p.maximum_daily_turnover:
            failures.append("maximum_daily_turnover")
        if proposal.side == OrderSide.BUY:
            if metrics["resulting_cash"] < p.minimum_cash_reserve:
                failures.append("minimum_cash_reserve")
            if metrics["concentration"] > p.max_position_percentage:
                failures.append("maximum_position_percentage")
            if metrics["concentration"] > p.max_symbol_concentration:
                failures.append("maximum_symbol_concentration")
            if metrics["sector_concentration"] > p.max_sector_concentration:
                failures.append("maximum_sector_concentration")
        return failures

    def _size_quantity(self, proposal: TradeProposal, portfolio: PortfolioSnapshot) -> int:
        """Find the largest compliant whole-share quantity without model judgment."""
        low = 0
        high = proposal.quantity
        while low < high:
            candidate = (low + high + 1) // 2
            if self._sizing_failures(proposal, portfolio, candidate):
                high = candidate - 1
            else:
                low = candidate
        return low

    def evaluate(self, proposal: TradeProposal, portfolio: PortfolioSnapshot, now) -> RiskDecision:
        p = self.policy
        observation = proposal.market_observation
        requested_quantity = proposal.quantity
        sized_quantity = self._size_quantity(proposal, portfolio)
        requested_sizing_failures = self._sizing_failures(
            proposal, portfolio, requested_quantity
        )
        evaluated_quantity = sized_quantity if sized_quantity > 0 else requested_quantity
        metrics = self._quantity_metrics(proposal, portfolio, evaluated_quantity)
        notional = metrics["notional"]
        resulting_cash = metrics["resulting_cash"]
        concentration = metrics["concentration"]
        sector_concentration = metrics["sector_concentration"]
        daily_turnover = metrics["daily_turnover"]
        sizing_passed = sized_quantity > 0
        if not sizing_passed:
            sizing_reason = (
                f"no positive whole-share quantity satisfies "
                f"{', '.join(requested_sizing_failures) or 'configured quantity limits'}"
            )
        elif sized_quantity < requested_quantity:
            sizing_reason = (
                f"requested quantity {requested_quantity} reduced to {sized_quantity} whole shares "
                f"to satisfy {', '.join(requested_sizing_failures)}"
            )
        else:
            sizing_reason = f"requested quantity {requested_quantity} requires no adjustment"
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
                "deterministic_order_sizing",
                sizing_passed,
                sizing_reason,
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
                f"order notional {notional} "
                f"{'is within' if notional <= p.maximum_order_notional else 'exceeds'} "
                f"limit {p.maximum_order_notional}",
            ),
            self._result(
                "maximum_daily_turnover",
                daily_turnover <= p.maximum_daily_turnover,
                f"resulting daily turnover {daily_turnover} "
                f"{'is within' if daily_turnover <= p.maximum_daily_turnover else 'exceeds'} "
                f"limit {p.maximum_daily_turnover}",
            ),
            self._result(
                "sufficient_holdings",
                proposal.side == OrderSide.BUY
                or requested_quantity <= portfolio.holdings.get(proposal.symbol, 0),
                "requested sell quantity does not exceed holdings"
                if proposal.side == OrderSide.BUY
                or requested_quantity <= portfolio.holdings.get(proposal.symbol, 0)
                else "requested sell quantity exceeds holdings",
            ),
            self._result(
                "minimum_cash_reserve",
                proposal.side == OrderSide.SELL or resulting_cash >= p.minimum_cash_reserve,
                f"resulting cash {resulting_cash} "
                f"{'meets' if proposal.side == OrderSide.SELL or resulting_cash >= p.minimum_cash_reserve else 'falls below'} "
                f"reserve {p.minimum_cash_reserve}",
            ),
        ]

        concentration_applies = proposal.side == OrderSide.BUY
        rules.extend(
            [
                self._result(
                    "maximum_position_percentage",
                    not concentration_applies or concentration <= p.max_position_percentage,
                    f"resulting position percentage {concentration:.4f} "
                    f"{'is within' if not concentration_applies or concentration <= p.max_position_percentage else 'exceeds'} "
                    f"limit {p.max_position_percentage}",
                ),
                self._result(
                    "maximum_symbol_concentration",
                    not concentration_applies or concentration <= p.max_symbol_concentration,
                    f"resulting symbol concentration {concentration:.4f} "
                    f"{'is within' if not concentration_applies or concentration <= p.max_symbol_concentration else 'exceeds'} "
                    f"limit {p.max_symbol_concentration}",
                ),
            ]
        )
        rules.append(
            self._result(
                "maximum_sector_concentration",
                not concentration_applies or sector_concentration <= p.max_sector_concentration,
                f"resulting sector concentration {sector_concentration:.4f} "
                f"{'is within' if not concentration_applies or sector_concentration <= p.max_sector_concentration else 'exceeds'} "
                f"limit {p.max_sector_concentration}",
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
                f"drawdown {drawdown:.4f} "
                f"{'is below' if drawdown < p.maximum_drawdown else 'meets or exceeds'} "
                f"kill-switch threshold {p.maximum_drawdown}",
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
            requested_quantity=requested_quantity,
            approved_quantity=sized_quantity if outcome != RiskOutcome.REJECTED else None,
        )
