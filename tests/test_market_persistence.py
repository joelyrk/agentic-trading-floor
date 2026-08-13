from datetime import datetime, timezone
from decimal import Decimal

from backend import accounts, database
from backend.accounts import Account
from backend.market.models import DataMode, MarketObservation, ObservationSource


def fake_observation(symbol: str) -> MarketObservation:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    return MarketObservation(
        symbol=symbol,
        price=Decimal("100"),
        currency="USD",
        market_timestamp=now,
        retrieved_at=now,
        source=ObservationSource.SIMULATOR,
        mode=DataMode.SIMULATED,
        is_stale=False,
        provider_endpoint="deterministic-simulator/v1",
    )


def test_order_and_valuation_persist_exact_observations(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "accounts.db")
    monkeypatch.setattr(database, "DB", db_path)
    database.initialize_database()
    monkeypatch.setattr(accounts, "get_market_observation", fake_observation)
    account = Account.get("Audit")

    account.buy_shares("aapl", 2, "test")
    account.calculate_portfolio_value()

    stored = database.read_market_observations("Audit")
    assert [row["usage_kind"] for row in stored].count("order") == 1
    assert [row["usage_kind"] for row in stored].count("valuation") == 2
    assert all(row["observation"]["symbol"] == "AAPL" for row in stored)
    transaction = Account.get("Audit").transactions[0]
    order_record = next(row for row in stored if row["usage_kind"] == "order")
    assert transaction.market_observation_id == order_record["id"]
    assert transaction.market_observation == fake_observation("AAPL")
