import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import mcp
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.accounts_client import account_server_params, read_accounts_resource
from backend.migrations import migrate


def test_account_client_reads_the_configured_database(tmp_path, monkeypatch) -> None:
    database = tmp_path / "live.db"
    migrate(str(database))
    account = {
        "name": "alice",
        "balance": 7500.0,
        "strategy": "Test strategy",
        "holdings": {"BRK.B": 5},
        "transactions": [],
        "portfolio_value_time_series": [],
    }
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO accounts(name, account) VALUES (?, ?)",
            ("alice", json.dumps(account)),
        )

    monkeypatch.setenv("ACCOUNTS_DB", str(database))
    monkeypatch.setenv("MARKET_DATA_MODE", "simulated")
    monkeypatch.setenv("MARKET_DATA_FALLBACK", "fail_closed")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))

    params = account_server_params()
    assert params["env"]["ACCOUNTS_DB"] == str(database)
    payload = json.loads(asyncio.run(read_accounts_resource("Alice")))
    assert payload["holdings"] == {"BRK.B": 5}


def test_account_mcp_exposes_no_trade_execution_tools(tmp_path) -> None:
    async def exercise() -> None:
        env = dict(os.environ)
        env["ACCOUNTS_DB"] = str(tmp_path / "accounts.db")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "backend.accounts_server"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
        )
        async with stdio_client(params) as streams:
            async with mcp.ClientSession(*streams) as session:
                await session.initialize()
                tool_names = {tool.name for tool in (await session.list_tools()).tools}
                assert tool_names == {"get_balance", "get_holdings", "change_strategy"}
                assert {"buy_shares", "sell_shares"}.isdisjoint(tool_names)

    asyncio.run(exercise())
