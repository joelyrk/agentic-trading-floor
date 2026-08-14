from datetime import datetime, timedelta, timezone

from backend.market_simulator import simulated_price


def test_price_is_deterministic_for_symbol_and_time() -> None:
    when = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert simulated_price("AAPL", when) == simulated_price("aapl", when)


def test_different_symbols_have_distinct_price_paths() -> None:
    when = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert simulated_price("AAPL", when) != simulated_price("MSFT", when)


def test_price_is_positive_and_moves_over_time() -> None:
    start = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    later = start + timedelta(hours=6)
    start_price = simulated_price("AAPL", start)
    later_price = simulated_price("AAPL", later)

    assert start_price > 0
    assert later_price > 0
    assert start_price != later_price
    assert start_price == round(start_price, 2)
    assert later_price == round(later_price, 2)
