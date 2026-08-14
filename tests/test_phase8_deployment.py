import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.access import ReadOnlyModeMiddleware
from backend.config import ApplicationSettings, validate_startup
from backend.demo import DEMO_SEED_VERSION, seed_demo_database


def test_seeded_demo_is_complete_and_idempotent(tmp_path) -> None:
    path = tmp_path / "demo.db"
    assert seed_demo_database(path) is True
    assert seed_demo_database(path) is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM execution_results").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cycle_metrics").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM demo_seed_state WHERE version=?", (DEMO_SEED_VERSION,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM service_health WHERE state != 'healthy'").fetchone()[
                0
            ]
            == 0
        )


def test_demo_middleware_rejects_mutations() -> None:
    app = FastAPI()
    app.add_middleware(ReadOnlyModeMiddleware, read_only=True)

    @app.get("/record")
    def read_record():
        return {"ok": True}

    @app.post("/record")
    def write_record():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/record").status_code == 200
    response = client.post("/record")
    assert response.status_code == 403
    assert response.json()["detail"] == "seeded demo mode is read-only"


def test_demo_mode_requires_simulated_data_and_refuses_scheduler(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("ACCOUNTS_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("MARKET_DATA_MODE", "end_of_day")
    monkeypatch.setenv("MASSIVE_API_KEY", "placeholder")
    with pytest.raises(ValueError, match="requires MARKET_DATA_MODE=simulated"):
        validate_startup("api")
    monkeypatch.setenv("MARKET_DATA_MODE", "simulated")
    monkeypatch.delenv("MASSIVE_API_KEY")
    with pytest.raises(ValueError, match="read-only and cannot run the scheduler"):
        validate_startup("scheduler")


def test_container_manifests_keep_browser_configuration_secret_free() -> None:
    compose = Path("compose.yaml").read_text()
    frontend = Path("frontend/Dockerfile").read_text()
    client = Path("frontend/src/api.ts").read_text()
    assert "APP_MODE: demo" in compose
    assert 'profiles: ["live"]' in compose
    assert "OPENAI_API_KEY" not in frontend
    assert "MASSIVE_API_KEY" not in frontend
    assert "TAVILY_API_KEY" not in frontend
    assert 'get("/api/runtime")' in client


def test_application_settings_make_read_only_mode_explicit(monkeypatch) -> None:
    monkeypatch.setenv("APP_MODE", "demo")
    settings = ApplicationSettings.from_env()
    assert settings.mode == "demo"
    assert settings.read_only is True
