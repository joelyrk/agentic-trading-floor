from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.market.massive import MassiveEodProvider
from backend.market.provider import (
    AuthenticationError,
    EmptyMarketDayError,
    EntitlementError,
    MalformedResponseError,
    MarketProviderError,
    ProviderTimeoutError,
    TransientProviderError,
)


class FixedClock:
    def __init__(self, now: datetime):
        self.value = now

    def now(self) -> datetime:
        return self.value


class FakeHttpError(Exception):
    def __init__(self, status_code: int, message: str | None = None):
        super().__init__(message or f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


class FakeClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def get_previous_close_agg(self, symbol: str):
        self.calls.append(symbol)
        if self.error:
            raise self.error
        return self.result


class ScriptedClient:
    def __init__(self, outcomes: list[object]):
        self.outcomes = iter(outcomes)
        self.calls: list[str] = []

    def get_previous_close_agg(self, symbol: str):
        self.calls.append(symbol)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakePoolManager:
    def __init__(self):
        self.connection_pool_kw = {"maxsize": 1}


def provider(
    client,
    now: datetime | None = None,
    *,
    max_retries: int = 0,
    sleeper=None,
) -> MassiveEodProvider:
    captured = {}

    def factory(api_key, **kwargs):
        captured.update(api_key=api_key, **kwargs)
        return client

    result = MassiveEodProvider(
        "key",
        FixedClock(now or datetime(2026, 8, 13, 12, tzinfo=timezone.utc)),
        timeout_seconds=3,
        max_retries=max_retries,
        client_factory=factory,
        sleeper=sleeper or (lambda seconds: None),
    )
    assert captured == {"api_key": "key", "connect_timeout": 3, "read_timeout": 3}
    return result


def test_massive_previous_close_success_uses_one_intentional_endpoint() -> None:
    stamp_ms = 1_765_411_400_000
    client = FakeClient([SimpleNamespace(close=199.75, timestamp=stamp_ms)])
    result = provider(client).observe("aapl")

    assert result.symbol == "AAPL"
    assert float(result.price) == 199.75
    assert result.provider_endpoint == "/v2/aggs/ticker/AAPL/prev"
    assert client.calls == ["AAPL"]


def test_massive_expands_the_shared_connection_pool_for_parallel_valuations() -> None:
    client = FakeClient([])
    client.client = FakePoolManager()

    provider(client)

    assert client.client.connection_pool_kw["maxsize"] == 10


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeHttpError(401), AuthenticationError),
        (FakeHttpError(403), EntitlementError),
        (TimeoutError(), ProviderTimeoutError),
    ],
)
def test_massive_normalizes_expected_failures(error: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected) as captured:
        provider(FakeClient(error=error)).observe("AAPL")

    assert f"exception={type(error).__name__}" in str(captured.value)


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_massive_retries_transient_http_failures_then_succeeds(status: int) -> None:
    sleeps = []
    result = [{"c": 202.5, "t": 1_765_411_400_000}]
    client = ScriptedClient([FakeHttpError(status), FakeHttpError(status), result])

    observation = provider(
        client,
        max_retries=2,
        sleeper=sleeps.append,
    ).observe("AAPL")

    assert float(observation.price) == 202.5
    assert client.calls == ["AAPL", "AAPL", "AAPL"]
    assert sleeps == [0.5, 1.0]


def test_massive_retries_timeouts_then_raises_with_safe_diagnostics() -> None:
    secret = "api-key=super-secret-value"
    client = ScriptedClient([TimeoutError(secret), TimeoutError(secret)])

    with pytest.raises(ProviderTimeoutError) as captured:
        provider(client, max_retries=1).observe("AAPL")

    assert client.calls == ["AAPL", "AAPL"]
    assert "status=unknown" in str(captured.value)
    assert "exception=TimeoutError" in str(captured.value)
    assert "super-secret-value" not in str(captured.value)


@pytest.mark.parametrize("status,expected", [(401, AuthenticationError), (403, EntitlementError)])
def test_massive_never_retries_authentication_or_entitlement_failures(
    status: int, expected: type[Exception]
) -> None:
    client = ScriptedClient([FakeHttpError(status, "api-key=super-secret-value")])

    with pytest.raises(expected) as captured:
        provider(client, max_retries=2).observe("AAPL")

    assert client.calls == ["AAPL"]
    assert f"status={status}" in str(captured.value)
    assert "super-secret-value" not in str(captured.value)


def test_massive_preserves_status_and_exception_class_for_non_retryable_failure() -> None:
    client = ScriptedClient([FakeHttpError(422, "request body with api-key=secret")])

    with pytest.raises(MarketProviderError) as captured:
        provider(client, max_retries=2).observe("AAPL")

    assert client.calls == ["AAPL"]
    assert "status=422" in str(captured.value)
    assert "exception=FakeHttpError" in str(captured.value)
    assert "secret" not in str(captured.value)


def test_massive_exhausts_transient_retries_with_typed_failure() -> None:
    client = ScriptedClient([FakeHttpError(503), FakeHttpError(503), FakeHttpError(503)])

    with pytest.raises(TransientProviderError, match="status=503"):
        provider(client, max_retries=2).observe("AAPL")

    assert client.calls == ["AAPL", "AAPL", "AAPL"]


def test_massive_rejects_malformed_payload() -> None:
    with pytest.raises(MalformedResponseError):
        provider(FakeClient([{"close": "not-a-price", "timestamp": "nope"}])).observe("AAPL")


def test_massive_reports_empty_market_day() -> None:
    with pytest.raises(EmptyMarketDayError):
        provider(FakeClient([])).observe("AAPL")


def test_weekend_uses_previous_trading_close_without_live_probe() -> None:
    sunday = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    friday_close_ms = int(datetime(2026, 8, 14, 20, tzinfo=timezone.utc).timestamp() * 1000)
    client = FakeClient([{"c": 202.5, "t": friday_close_ms}])
    adapter = provider(client, sunday)

    result = adapter.observe("MSFT")

    assert result.market_timestamp.weekday() == 4
    assert adapter.is_market_open() is False
    assert client.calls == ["MSFT"]


def test_holiday_gap_preserves_provider_previous_close_timestamp() -> None:
    # Friday 2026-07-03 is the observed Independence Day market holiday, so
    # Massive's previous-close result remains Thursday's daily aggregate.
    sunday = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    thursday_close = datetime(2026, 7, 2, 20, tzinfo=timezone.utc)
    client = FakeClient([{"c": 155.25, "t": int(thursday_close.timestamp() * 1000)}])

    result = provider(client, sunday).observe("NVDA")

    assert result.market_timestamp == thursday_close
    assert result.retrieved_at == sunday
