"""Transactional, versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = (
    Migration(
        1,
        "accounts_market_and_logs",
        """
        CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, account TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, datetime DATETIME,
            type TEXT, message TEXT
        );
        CREATE TABLE IF NOT EXISTS market_observations (
            id TEXT PRIMARY KEY, account_name TEXT NOT NULL, usage_kind TEXT NOT NULL
            CHECK (usage_kind IN ('valuation', 'order', 'proposal')), related_id TEXT NOT NULL,
            symbol TEXT NOT NULL, observation TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_market_observations_account_usage
        ON market_observations(account_name, usage_kind, recorded_at);
    """,
    ),
    Migration(
        2,
        "research_decision_and_execution",
        """
        CREATE TABLE IF NOT EXISTS research_briefs (
            research_id TEXT PRIMARY KEY, account_name TEXT NOT NULL, brief TEXT NOT NULL,
            decision_cutoff TEXT NOT NULL, researcher_prompt_version TEXT NOT NULL,
            trader_prompt_version TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trade_proposals (
            proposal_id TEXT PRIMARY KEY, account_name TEXT NOT NULL, proposal TEXT NOT NULL,
            created_at TEXT NOT NULL, research_id TEXT,
            FOREIGN KEY(research_id) REFERENCES research_briefs(research_id)
        );
        CREATE INDEX IF NOT EXISTS idx_research_account_cutoff
        ON research_briefs(account_name, decision_cutoff);
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
            account_name TEXT NOT NULL, outcome TEXT NOT NULL, decision TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES trade_proposals(proposal_id)
        );
        CREATE TABLE IF NOT EXISTS paper_orders (
            order_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL UNIQUE, account_name TEXT NOT NULL,
            order_payload TEXT NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL,
            FOREIGN KEY(decision_id) REFERENCES risk_decisions(decision_id)
        );
        CREATE TABLE IF NOT EXISTS execution_results (
            execution_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
            result TEXT NOT NULL, executed_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES paper_orders(order_id)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_account_submitted
        ON paper_orders(account_name, submitted_at);
    """,
    ),
    Migration(
        3,
        "health_and_cycle_telemetry",
        """
        CREATE TABLE IF NOT EXISTS service_health (
            name TEXT PRIMARY KEY, state TEXT NOT NULL, required INTEGER NOT NULL,
            last_success TEXT, last_error TEXT, error_summary TEXT, latency_ms REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0, circuit_open_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cycle_metrics (
            cycle_id TEXT PRIMARY KEY, account_name TEXT NOT NULL, run_id TEXT,
            scenario_id TEXT, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
            market_mode TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            status TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd TEXT NOT NULL DEFAULT '0', latency_ms REAL,
            error_summary TEXT, decision_ids TEXT NOT NULL DEFAULT '[]', budget TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cycle_metrics_started ON cycle_metrics(started_at);
        CREATE TABLE IF NOT EXISTS decision_telemetry (
            decision_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, trace_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(cycle_id) REFERENCES cycle_metrics(cycle_id)
        );
    """,
    ),
    Migration(
        4,
        "replay_sessions",
        """
        CREATE TABLE IF NOT EXISTS replay_sessions (
            replay_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, strategy TEXT NOT NULL,
            seed INTEGER NOT NULL, status TEXT NOT NULL
            CHECK (status IN ('decision_complete', 'outcome_revealed')),
            decision_payload TEXT NOT NULL, outcome_payload TEXT,
            created_at TEXT NOT NULL, revealed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_replay_sessions_created ON replay_sessions(created_at);
    """,
    ),
    Migration(
        5,
        "audited_agent_runs",
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
            status TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')),
            requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
            requested_by TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            market_symbol TEXT NOT NULL, market_timestamp TEXT NOT NULL,
            market_retrieved_at TEXT NOT NULL, market_mode TEXT NOT NULL,
            error_summary TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_one_active
        ON agent_runs((1)) WHERE status IN ('queued', 'running');
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_market_snapshot
        ON agent_runs(market_mode, market_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_requested
        ON agent_runs(requested_at);
    """,
    ),
    Migration(
        6,
        "active_service_inventory",
        """
        SELECT 1;
    """,
    ),
    Migration(
        7,
        "audited_agent_run_retries",
        """
        DROP INDEX IF EXISTS idx_agent_runs_market_snapshot;
        CREATE INDEX IF NOT EXISTS idx_agent_runs_market_snapshot
        ON agent_runs(market_mode, market_timestamp);
    """,
    ),
    Migration(
        8,
        "partial_agent_run_outcomes",
        """
        DROP INDEX IF EXISTS idx_agent_runs_one_active;
        DROP INDEX IF EXISTS idx_agent_runs_market_snapshot;
        DROP INDEX IF EXISTS idx_agent_runs_requested;
        ALTER TABLE agent_runs RENAME TO agent_runs_v7;
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY,
            trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
            status TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'succeeded', 'partial_success', 'failed', 'interrupted')),
            requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
            requested_by TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            market_symbol TEXT NOT NULL, market_timestamp TEXT NOT NULL,
            market_retrieved_at TEXT NOT NULL, market_mode TEXT NOT NULL,
            error_summary TEXT, retry_of TEXT
        );
        INSERT INTO agent_runs
            (run_id, trigger, status, requested_at, started_at, completed_at,
             requested_by, idempotency_key, market_symbol, market_timestamp,
             market_retrieved_at, market_mode, error_summary, retry_of)
        SELECT run_id, trigger, status, requested_at, started_at, completed_at,
               requested_by, idempotency_key, market_symbol, market_timestamp,
               market_retrieved_at, market_mode, error_summary, retry_of
        FROM agent_runs_v7;
        DROP TABLE agent_runs_v7;
        CREATE UNIQUE INDEX idx_agent_runs_one_active
        ON agent_runs((1)) WHERE status IN ('queued', 'running');
        CREATE INDEX idx_agent_runs_market_snapshot
        ON agent_runs(market_mode, market_timestamp);
        CREATE INDEX idx_agent_runs_requested
        ON agent_runs(requested_at);
    """,
    ),
    Migration(
        9,
        "backfill_partial_agent_run_outcomes",
        """
        UPDATE agent_runs
        SET status='partial_success'
        WHERE status='failed'
          AND EXISTS (
              SELECT 1 FROM cycle_metrics
              WHERE cycle_metrics.run_id=agent_runs.run_id
                AND cycle_metrics.status='succeeded'
          );
    """,
    ),
    Migration(
        10,
        "notification_outbox",
        """
        CREATE TABLE notification_outbox (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL
            CHECK (event_type IN ('decision_outcome', 'run_summary')),
            run_id TEXT NOT NULL, decision_id TEXT, message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, last_attempt_at TEXT, sent_at TEXT,
            error_summary TEXT,
            FOREIGN KEY(run_id) REFERENCES agent_runs(run_id),
            FOREIGN KEY(decision_id) REFERENCES risk_decisions(decision_id)
        );
        CREATE INDEX idx_notification_outbox_pending
        ON notification_outbox(status, created_at);
    """,
    ),
    Migration(
        11,
        "cycle_usage_availability",
        """
        ALTER TABLE cycle_metrics ADD COLUMN usage_status TEXT NOT NULL DEFAULT 'available'
        CHECK (usage_status IN ('available', 'unavailable'));
        UPDATE cycle_metrics SET usage_status='unavailable'
        WHERE requests > 0 AND total_tokens = 0;
    """,
    ),
)


def _ensure_legacy_columns(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before migrations without losing their records."""
    proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_proposals)")}
    if proposal_columns and "research_id" not in proposal_columns:
        conn.execute("ALTER TABLE trade_proposals ADD COLUMN research_id TEXT")
    health_columns = {row[1] for row in conn.execute("PRAGMA table_info(service_health)")}
    if health_columns and "attempt_count" not in health_columns:
        conn.execute(
            "ALTER TABLE service_health ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        )
    if health_columns and "failure_count" not in health_columns:
        conn.execute(
            "ALTER TABLE service_health ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
        )
    if health_columns and "active" not in health_columns:
        conn.execute("ALTER TABLE service_health ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    if run_columns and "retry_of" not in run_columns:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN retry_of TEXT")


def migrate(path: str) -> list[int]:
    """Apply each missing migration atomically and return applied versions."""
    applied: list[int] = []
    with sqlite3.connect(path, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
               DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""
        )
        conn.commit()
        for migration in MIGRATIONS:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (migration.version,)
                ).fetchone():
                    conn.commit()
                    continue
                for statement in migration.sql.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                _ensure_legacy_columns(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(migration.version)
    return applied


def current_version(path: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])
