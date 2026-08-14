import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

from backend.market.models import MarketObservation
from backend.migrations import migrate

load_dotenv()

DB = os.getenv("ACCOUNTS_DB", "accounts.db")


def initialize_database(path: str | None = None) -> None:
    """Apply all pending schema migrations to a new or existing account DB."""
    migrate(path or DB)


initialize_database()


def write_account(name, account_dict):
    json_data = json.dumps(account_dict)
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO accounts (name, account)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET account=excluded.account
        """,
            (name.lower(), json_data),
        )
        conn.commit()


def read_account(name):
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT account FROM accounts WHERE name = ?", (name.lower(),))
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None


def write_log(name: str, type: str, message: str):
    """
    Write a log entry to the logs table.

    Args:
        name (str): The name associated with the log
        type (str): The type of log entry
        message (str): The log message
    """
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO logs (name, datetime, type, message)
            VALUES (?, datetime('now'), ?, ?)
        """,
            (name.lower(), type, message),
        )
        conn.commit()


def write_market_observation(
    account_name: str,
    usage_kind: str,
    related_id: str,
    observation: MarketObservation,
) -> str:
    """Persist the exact observation used by a valuation, proposal, or order."""
    observation_id = str(uuid4())
    payload = observation.model_dump_json()
    recorded_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB) as conn:
        conn.execute(
            """
            INSERT INTO market_observations
                (id, account_name, usage_kind, related_id, symbol, observation, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                account_name.lower(),
                usage_kind,
                related_id,
                observation.symbol,
                payload,
                recorded_at,
            ),
        )
        conn.commit()
    return observation_id


def read_market_observations(
    account_name: str,
    usage_kind: str | None = None,
    related_id: str | None = None,
) -> list[dict]:
    """Read audit records, with filters composed using bound parameters."""
    clauses = ["account_name = ?"]
    params: list[str] = [account_name.lower()]
    if usage_kind is not None:
        clauses.append("usage_kind = ?")
        params.append(usage_kind)
    if related_id is not None:
        clauses.append("related_id = ?")
        params.append(related_id)
    query = (
        "SELECT id, usage_kind, related_id, observation, recorded_at "
        "FROM market_observations WHERE " + " AND ".join(clauses) + " ORDER BY recorded_at"
    )
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row[0],
            "usage_kind": row[1],
            "related_id": row[2],
            "observation": json.loads(row[3]),
            "recorded_at": row[4],
        }
        for row in rows
    ]


def read_log(name: str, last_n=10):
    """
    Read the most recent log entries for a given name.

    Args:
        name (str): The name to retrieve logs for
        last_n (int): Number of most recent entries to retrieve

    Returns:
        list: A list of tuples containing (datetime, type, message)
    """
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT datetime, type, message FROM logs 
            WHERE name = ? 
            ORDER BY datetime DESC
            LIMIT ?
        """,
            (name.lower(), last_n),
        )

        return reversed(cursor.fetchall())
