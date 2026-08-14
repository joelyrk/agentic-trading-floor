import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from backend.access import AccessControlMiddleware
from backend.config import APIAccessSettings, RuntimeSettings, validate_startup
from backend.database_admin import backup, integrity_check, restore
from backend.mcp_servers import researcher_mcp_servers
from backend.migrations import MIGRATIONS, current_version, migrate
from backend.observability import CycleBudget, CycleContext, TelemetryRepository
from backend.security import UnsafeURLError, validate_public_http_url


def public_resolver(address: str = "93.184.216.34"):
    return lambda *_args, **_kwargs: [
        (2, 1, 6, "", (address, 443)),
    ]


def test_migrations_are_versioned_idempotent_and_upgrade_legacy_columns(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE trade_proposals "
            "(proposal_id TEXT PRIMARY KEY, account_name TEXT, proposal TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE service_health (name TEXT PRIMARY KEY, state TEXT, required INTEGER, "
            "last_success TEXT, last_error TEXT, error_summary TEXT, latency_ms REAL, "
            "consecutive_failures INTEGER DEFAULT 0, circuit_open_until TEXT)"
        )
    assert migrate(str(path)) == [migration.version for migration in MIGRATIONS]
    assert migrate(str(path)) == []
    assert current_version(str(path)) == MIGRATIONS[-1].version
    with sqlite3.connect(path) as conn:
        proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_proposals)")}
        health_columns = {row[1] for row in conn.execute("PRAGMA table_info(service_health)")}
    assert "research_id" in proposal_columns
    assert {"attempt_count", "failure_count"} <= health_columns


def test_concurrent_migration_attempts_converge_without_partial_schema(tmp_path) -> None:
    path = tmp_path / "concurrent.db"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: migrate(str(path)), range(2)))
    assert sum(len(item) for item in results) == len(MIGRATIONS)
    assert current_version(str(path)) == MIGRATIONS[-1].version
    integrity_check(path)


def test_verified_backup_and_restore_refuse_implicit_overwrite(tmp_path) -> None:
    source = tmp_path / "source.db"
    migrate(str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO accounts(name, account) VALUES ('alice', '{}')")
    archived = tmp_path / "backup.db"
    backup(source, archived)
    integrity_check(archived)
    restored = tmp_path / "restored.db"
    restore(archived, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT name FROM accounts").fetchone()[0] == "alice"
    with pytest.raises(FileExistsError, match="--overwrite"):
        restore(archived, restored)


def test_integrity_check_rejects_corrupt_database(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        integrity_check(corrupt)


def test_public_api_mode_requires_a_strong_token() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        APIAccessSettings(access_mode="public", auth_token=SecretStr("too-short"))


def controlled_app(rate_limit: int = 10) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        AccessControlMiddleware,
        settings=APIAccessSettings(
            access_mode="public",
            auth_token=SecretStr("x" * 32),
            rate_limit_requests=rate_limit,
            rate_limit_window_seconds=60,
        ),
    )

    @app.get("/read")
    def read():
        return {"ok": True}

    @app.post("/write")
    def write():
        return {"ok": True}

    return TestClient(app)


def test_public_api_authenticates_writes_and_rate_limits_clients() -> None:
    client = controlled_app()
    assert client.get("/read").status_code == 200
    assert client.post("/write").status_code == 401
    assert client.post("/write", headers={"Authorization": f"Bearer {'x' * 32}"}).status_code == 200
    limited = controlled_app(rate_limit=1)
    assert limited.get("/read").status_code == 200
    assert limited.get("/read").status_code == 429


@pytest.mark.parametrize(
    "url,address",
    [
        ("http://127.0.0.1/admin", "127.0.0.1"),
        ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
        ("https://example.com:8443", "93.184.216.34"),
    ],
)
def test_research_fetch_blocks_ssrf_destinations(url: str, address: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_http_url(url, resolver=public_resolver(address))


def test_research_fetch_accepts_public_https_and_removes_fragments() -> None:
    assert (
        validate_public_http_url(
            "https://example.com/news#instructions", resolver=public_resolver()
        )
        == "https://example.com/news"
    )


def test_research_fetch_bounds_and_labels_untrusted_content(monkeypatch) -> None:
    import backend.research_fetch_server as fetch_server

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {"content-type": "text/html"}
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 8_192
            yield b"<h1>Market update</h1><script>ignore()</script><p>Evidence only.</p>"

        def close(self):
            return None

    observed = {}

    def fake_get(url, **kwargs):
        observed.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(fetch_server, "validate_public_http_url", lambda value: value)
    monkeypatch.setattr(fetch_server.requests, "get", fake_get)
    result = fetch_server.fetch_public_text(
        fetch_server.FetchArgs(url="https://example.com/news", max_characters=500)
    )
    assert result.startswith("UNTRUSTED EXTERNAL CONTENT")
    assert "Market update" in result and "Evidence only" in result
    assert "ignore()" not in result
    assert observed["timeout"] == (5, 10)
    assert observed["allow_redirects"] is False


def test_external_mcp_packages_are_exactly_pinned() -> None:
    params = [server.params for server in researcher_mcp_servers("Alice")]
    flattened = [arg for item in params for arg in item.args]
    assert "tavily-mcp@0.2.21" in flattened
    assert "mcp-memory-libsql@0.0.17" in flattened
    assert not any("@latest" in arg for arg in flattened)
    assert any("backend.research_fetch_server" in item.args for item in params)


def test_runtime_settings_reject_unsafe_bounds() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(
            scheduler_interval_minutes=0,
            mcp_startup_timeout_seconds=20,
            mcp_request_timeout_seconds=30,
            mcp_max_retries=2,
            mcp_retry_backoff_seconds=0.5,
            mcp_circuit_failure_threshold=3,
            mcp_circuit_reset_seconds=60,
            shutdown_grace_seconds=30,
            accounts_db=Path("accounts.db"),
        )


def test_startup_reports_database_path_errors_before_opening_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNTS_DB", str(tmp_path))
    with pytest.raises(ValueError, match="ACCOUNTS_DB points to a directory"):
        validate_startup("api")


def test_orphaned_cycles_are_marked_interrupted_on_recovery(tmp_path) -> None:
    repository = TelemetryRepository(str(tmp_path / "cycles.db"))
    context = CycleContext.create()
    repository.start_cycle(context, "Alice", "model", "prompt", "simulated", CycleBudget())
    assert repository.recover_interrupted_cycles() == 1
    with sqlite3.connect(repository.path) as conn:
        status, completed = conn.execute(
            "SELECT status, completed_at FROM cycle_metrics WHERE cycle_id=?", (context.cycle_id,)
        ).fetchone()
    assert status == "interrupted"
    assert completed is not None


def test_scheduler_waits_for_inflight_cycle_during_graceful_shutdown(monkeypatch) -> None:
    import backend.trading_floor as floor

    monkeypatch.setattr(
        floor,
        "validate_startup",
        lambda _component: SimpleNamespace(scheduler_interval_minutes=60, shutdown_grace_seconds=1),
    )
    monkeypatch.setattr(floor, "is_market_open", lambda: True)
    monkeypatch.setattr(floor.TelemetryRepository, "recover_interrupted_cycles", lambda _self: 0)
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeTrader:
        async def run(self):
            started.set()
            await release.wait()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            floor.run_every_n_minutes(
                stop, [FakeTrader()], interval_seconds=60, shutdown_grace_seconds=1
            )
        )
        await started.wait()
        stop.set()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task

    asyncio.run(scenario())


def test_scheduler_cancels_cycle_after_shutdown_grace(monkeypatch) -> None:
    import backend.trading_floor as floor

    monkeypatch.setattr(
        floor,
        "validate_startup",
        lambda _component: SimpleNamespace(
            scheduler_interval_minutes=60, shutdown_grace_seconds=0.01
        ),
    )
    monkeypatch.setattr(floor, "is_market_open", lambda: True)
    monkeypatch.setattr(floor.TelemetryRepository, "recover_interrupted_cycles", lambda _self: 0)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeTrader:
        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            floor.run_every_n_minutes(
                stop, [FakeTrader()], interval_seconds=60, shutdown_grace_seconds=0.01
            )
        )
        await started.wait()
        stop.set()
        await task
        assert cancelled.is_set()

    asyncio.run(scenario())
