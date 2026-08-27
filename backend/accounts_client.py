"""Credential-safe, supervised client for the paper-account MCP resources."""

import os

from .mcp_servers import (
    ObservedMCPServerStdio,
    local_server_params,
    market_runtime_environment,
)


def account_server_params() -> dict:
    """Pass only required account and valuation settings to the isolated server."""
    environment = market_runtime_environment()
    environment["ACCOUNTS_DB"] = os.getenv("ACCOUNTS_DB", "accounts.db")
    return local_server_params(
        "backend.accounts_server",
        environment,
    )


async def _read(uri: str) -> str:
    async with ObservedMCPServerStdio(account_server_params(), name="paper-accounts") as server:
        result = await server.read_resource(uri)
        if not result.contents or not hasattr(result.contents[0], "text"):
            raise RuntimeError("paper-accounts returned an empty resource")
        return result.contents[0].text


async def read_accounts_resource(name: str) -> str:
    return await _read(f"accounts://accounts_server/{name}")


async def read_account_snapshot_resource(name: str) -> str:
    return await _read(f"accounts://snapshot/{name}")


async def read_strategy_resource(name: str) -> str:
    return await _read(f"accounts://strategy/{name}")
