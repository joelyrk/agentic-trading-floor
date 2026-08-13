from mcp.server.fastmcp import FastMCP
from .market import MarketObservation, MarketStatus, get_market_service

service = get_market_service()  # Validate mode/credential capability at server startup.

mcp = FastMCP("agentic-trading-floor-market")

@mcp.tool()
async def lookup_market_observation(symbol: str) -> MarketObservation:
    """Return an attributed point-in-time price observation for a stock symbol.

    Args:
        symbol: Uppercase US equity ticker symbol.
    """
    return service.observe(symbol)


@mcp.tool()
async def market_data_status() -> MarketStatus:
    """Return the active provider, data mode, freshness, and degraded state."""
    return service.status()

if __name__ == "__main__":
    mcp.run(transport='stdio')
