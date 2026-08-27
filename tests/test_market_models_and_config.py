from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.market.config import MarketSettings
from backend.market.models import (
    DataMode,
    FallbackPolicy,
    MarketObservation,
    ObservationSource,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def observation(**updates) -> MarketObservation:
    values = {
        "symbol": "aapl",
        "price": Decimal("201.25"),
        "currency": "usd",
        "market_timestamp": NOW,
        "retrieved_at": NOW,
        "source": ObservationSource.MASSIVE,
        "mode": DataMode.END_OF_DAY,
        "is_stale": False,
        "provider_endpoint": "/v2/aggs/ticker/AAPL/prev",
    }
    values.update(updates)
    return MarketObservation(**values)


def test_observation_normalizes_symbol_and_currency() -> None:
    result = observation()
    assert result.symbol == "AAPL"
    assert result.currency == "USD"


@pytest.mark.parametrize(
    "updates",
    [
        {"price": 0},
        {"symbol": "bad symbol"},
        {"market_timestamp": datetime(2026, 8, 13, 13, tzinfo=timezone.utc)},
        {"retrieved_at": datetime(2026, 8, 13, 12)},
    ],
)
def test_observation_rejects_invalid_boundary_data(updates: dict) -> None:
    with pytest.raises(ValidationError):
        observation(**updates)


def test_settings_require_credentials_for_eod() -> None:
    with pytest.raises(ValidationError, match="requires MASSIVE_API_KEY"):
        MarketSettings(
            mode=DataMode.END_OF_DAY,
            fallback_policy=FallbackPolicy.FAIL_CLOSED,
            freshness_threshold_seconds=60,
            request_timeout_seconds=5,
        )


@pytest.mark.parametrize("mode", [DataMode.DELAYED, DataMode.REAL_TIME])
def test_settings_reject_unsupported_entitlements(mode: DataMode) -> None:
    with pytest.raises(ValidationError, match="unavailable"):
        MarketSettings(
            mode=mode,
            fallback_policy=FallbackPolicy.FAIL_CLOSED,
            massive_api_key="secret",
            freshness_threshold_seconds=60,
            request_timeout_seconds=5,
        )


def test_simulator_must_be_selected_without_a_fallback_policy() -> None:
    with pytest.raises(ValidationError, match="only apply"):
        MarketSettings(
            mode=DataMode.SIMULATED,
            fallback_policy=FallbackPolicy.EXPLICIT_SIMULATOR,
            freshness_threshold_seconds=60,
            request_timeout_seconds=5,
        )


def test_settings_read_bounded_massive_retry_configuration(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_DATA_MODE", "end_of_day")
    monkeypatch.setenv("MARKET_DATA_FALLBACK", "fail_closed")
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_DATA_MAX_RETRIES", "3")
    monkeypatch.setenv("MARKET_DATA_RETRY_BACKOFF_SECONDS", "0.25")

    settings = MarketSettings.from_env()

    assert settings.max_retries == 3
    assert settings.retry_backoff_seconds == 0.25


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_retries", 11), ("retry_backoff_seconds", 31)],
)
def test_settings_reject_unsafe_massive_retry_bounds(field: str, value: int) -> None:
    values = {
        "mode": DataMode.END_OF_DAY,
        "fallback_policy": FallbackPolicy.FAIL_CLOSED,
        "massive_api_key": "test-key",
        "freshness_threshold_seconds": 60,
        "request_timeout_seconds": 5,
        field: value,
    }

    with pytest.raises(ValidationError):
        MarketSettings(**values)
