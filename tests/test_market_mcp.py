import asyncio
import os
import sys
from pathlib import Path

import mcp
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client


def test_market_mcp_schema_and_full_stdio_handshake(tmp_path) -> None:
    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            MARKET_DATA_MODE="simulated",
            MARKET_DATA_FALLBACK="fail_closed",
            ACCOUNTS_DB=str(tmp_path / "accounts.db"),
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "backend.market_server"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
        )
        async with stdio_client(params) as streams:
            async with mcp.ClientSession(*streams) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "agentic-trading-floor-market"
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools) == {"lookup_market_observation", "market_data_status"}
                schema = tools["lookup_market_observation"].outputSchema
                assert schema is not None
                assert "symbol" in schema["properties"]
                result = await session.call_tool(
                    "lookup_market_observation", {"symbol": "aapl"}
                )
                assert result.isError is False
                assert result.structuredContent["source"] == "simulator"
                assert result.structuredContent["mode"] == "simulated"

    asyncio.run(exercise())
