import sqlite3
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

from backend.market.models import MarketObservation

load_dotenv(override=True)

DB = os.getenv("ACCOUNTS_DB", "accounts.db")


def initialize_database(path: str | None = None) -> None:
    """Create additive tables/indexes for a new or existing paper-account DB."""
    with sqlite3.connect(path or DB) as conn:
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, account TEXT)')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            datetime DATETIME,
            type TEXT,
            message TEXT
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS market_observations (
            id TEXT PRIMARY KEY,
            account_name TEXT NOT NULL,
            usage_kind TEXT NOT NULL CHECK (usage_kind IN ('valuation', 'order', 'proposal')),
            related_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            observation TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_market_observations_account_usage
        ON market_observations(account_name, usage_kind, recorded_at)
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_proposals (
            proposal_id TEXT PRIMARY KEY,
            account_name TEXT NOT NULL,
            proposal TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE,
            account_name TEXT NOT NULL,
            outcome TEXT NOT NULL,
            decision TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES trade_proposals(proposal_id)
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS paper_orders (
            order_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL UNIQUE,
            account_name TEXT NOT NULL,
            order_payload TEXT NOT NULL,
            status TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            FOREIGN KEY(decision_id) REFERENCES risk_decisions(decision_id)
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS execution_results (
            execution_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL UNIQUE,
            result TEXT NOT NULL,
            executed_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES paper_orders(order_id)
        )
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_orders_account_submitted
        ON paper_orders(account_name, submitted_at)
        ''')
        conn.commit()


initialize_database()

def write_account(name, account_dict):
    json_data = json.dumps(account_dict)
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO accounts (name, account)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET account=excluded.account
        ''', (name.lower(), json_data))
        conn.commit()

def read_account(name):
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT account FROM accounts WHERE name = ?', (name.lower(),))
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
    now = datetime.now().isoformat()
    
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO logs (name, datetime, type, message)
            VALUES (?, datetime('now'), ?, ?)
        ''', (name.lower(), type, message))
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
            '''
            INSERT INTO market_observations
                (id, account_name, usage_kind, related_id, symbol, observation, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
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
        "FROM market_observations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY recorded_at"
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
        cursor.execute('''
            SELECT datetime, type, message FROM logs 
            WHERE name = ? 
            ORDER BY datetime DESC
            LIMIT ?
        ''', (name.lower(), last_n))
        
        return reversed(cursor.fetchall())
