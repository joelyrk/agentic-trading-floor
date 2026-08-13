"""Dependency-free, deterministic portfolio and quality metrics."""

from decimal import Decimal, localcontext

from .models import AggregateMetrics, ScenarioMetrics


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 28
        return value.sqrt()


def aggregate(rows: list[ScenarioMetrics]) -> AggregateMetrics:
    returns = [row.portfolio_return for row in rows]
    benchmarks = [row.benchmark_return for row in rows]
    equity = Decimal("1")
    benchmark_equity = Decimal("1")
    peak = Decimal("1")
    max_drawdown = Decimal("0")
    for value, benchmark in zip(returns, benchmarks, strict=True):
        equity *= Decimal("1") + value
        benchmark_equity *= Decimal("1") + benchmark
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    mean = _mean(returns)
    variance = _mean([(value - mean) ** 2 for value in returns])
    volatility = _sqrt(variance) * _sqrt(Decimal("252")) if returns else Decimal("0")
    sharpe = (mean / _sqrt(variance)) * _sqrt(Decimal("252")) if variance > 0 else None
    count = Decimal(len(rows) or 1)
    return AggregateMetrics(
        total_return=equity - 1,
        benchmark_return=benchmark_equity - 1,
        benchmark_relative_return=equity - benchmark_equity,
        annualized_volatility=volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        turnover=_mean([row.turnover for row in rows]),
        win_rate=sum(row.won for row in rows) / count,
        decision_validity=sum(row.decision_valid for row in rows) / count,
        citation_validity=sum(row.citation_valid for row in rows) / count,
        tool_success_rate=sum(row.tool_success for row in rows) / count,
        average_latency_ms=_mean([Decimal(row.latency_ms) for row in rows]),
        model_api_cost_usd=sum((row.model_cost_usd for row in rows), Decimal("0")),
    )
