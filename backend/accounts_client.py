"""Credential-safe, supervised client for the paper-account MCP resources."""

from .mcp_servers import ObservedMCPServerStdio, local_server_params


PARAMS = local_server_params("backend.accounts_server")


async def _read(uri: str) -> str:
    async with ObservedMCPServerStdio(PARAMS, name="paper-accounts") as server:
        result = await server.read_resource(uri)
        if not result.contents or not hasattr(result.contents[0], "text"):
            raise RuntimeError("paper-accounts returned an empty resource")
        return result.contents[0].text


async def read_accounts_resource(name: str) -> str:
    return await _read(f"accounts://accounts_server/{name}")


async def read_strategy_resource(name: str) -> str:
    return await _read(f"accounts://strategy/{name}")
