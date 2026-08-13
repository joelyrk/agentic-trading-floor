import os
from pathlib import Path
from dotenv import load_dotenv
from agents.mcp import MCPServerStdio, create_static_tool_filter

load_dotenv(override=True)

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
tavily_env = {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY")}
TIMEOUT = 120

# Traders always see the narrow, typed project-owned market server. Provider
# credentials and fallback policy stay behind this boundary.
market_params = {
    "command": "uv",
    "args": ["run", "-m", "backend.market_server"],
    "cwd": PROJECT_DIR,
}


def trader_mcp_servers() -> list[MCPServerStdio]:
    """Model-facing tools cannot mutate accounts; execution happens after structured output."""
    params = [
        {"command": "uv", "args": ["run", "-m", "backend.push_server"], "cwd": PROJECT_DIR},
        market_params,
    ]
    return [MCPServerStdio(p, client_session_timeout_seconds=TIMEOUT) for p in params]


def researcher_mcp_servers(name: str) -> list[MCPServerStdio]:
    """The researcher's MCP servers: Fetch, Tavily web search and Memory.

    Tavily's server offers several tools; we restrict it to web search so the
    researcher reaches for plain search rather than its heavier crawl or deep-research tools.
    """
    fetch = MCPServerStdio(
        {"command": "uvx", "args": ["--with", "mcp<2","mcp-server-fetch"]},
        client_session_timeout_seconds=TIMEOUT,
    )
    search = MCPServerStdio(
        {"command": "npx", "args": ["-y", "tavily-mcp@latest"], "env": tavily_env},
        client_session_timeout_seconds=TIMEOUT,
        tool_filter=create_static_tool_filter(allowed_tool_names=["tavily_search"]),
    )
    memory = MCPServerStdio(
        {
            "command": "npx",
            "args": ["-y", "mcp-memory-libsql"],
            "env": {"LIBSQL_URL": f"file:./memory/{name}.db"},
        },
        client_session_timeout_seconds=TIMEOUT,
    )
    return [fetch, search, memory]
