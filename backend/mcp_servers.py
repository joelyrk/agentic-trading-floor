"""Stable, supervised MCP server definitions used by live agents."""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from time import monotonic

from agents.mcp import MCPServerStdio
from dotenv import load_dotenv
from mcp.client.stdio import stdio_client

from .config import RuntimeSettings
from .observability import TelemetryRepository, measured, safe_error

load_dotenv()

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
_runtime = RuntimeSettings.from_env()
TIMEOUT = _runtime.mcp_request_timeout_seconds
STARTUP_TIMEOUT = _runtime.mcp_startup_timeout_seconds
RETRIES = _runtime.mcp_max_retries
BACKOFF = _runtime.mcp_retry_backoff_seconds
CIRCUIT_THRESHOLD = _runtime.mcp_circuit_failure_threshold
CIRCUIT_RESET_SECONDS = _runtime.mcp_circuit_reset_seconds

telemetry = TelemetryRepository()
ACTIVE_SERVICE_NAMES = (
    "paper-accounts",
    "notifications",
    "market-data",
    "research-search",
)
for _service_name in ACTIVE_SERVICE_NAMES:
    telemetry.register_service(_service_name, required=True)
telemetry.retire_services_except(ACTIVE_SERVICE_NAMES)


class CircuitOpenError(RuntimeError):
    pass


@asynccontextmanager
async def _captured_stdio(params, server):
    """Capture stderr in a mode-0600 temporary file, then retain only a redacted tail."""
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        try:
            async with stdio_client(params, errlog=diagnostics) as streams:
                yield streams
        finally:
            diagnostics.flush()
            size = diagnostics.seek(0, os.SEEK_END)
            diagnostics.seek(max(0, size - 8192))
            raw_tail = diagnostics.read().decode("utf-8", errors="replace")
            if raw_tail.strip():
                server._last_diagnostic = safe_error(raw_tail)


class ObservedMCPServerStdio(MCPServerStdio):
    """MCP stdio client with startup probing, redacted diagnostics, and circuit state."""

    def __init__(
        self,
        params,
        *,
        name: str,
        required: bool = True,
        tool_filter=None,
        telemetry_repository: TelemetryRepository | None = None,
    ):
        self.required = required
        self.telemetry = telemetry_repository or telemetry
        self._last_diagnostic: str | None = None
        self.telemetry.register_service(name, required)
        super().__init__(
            params,
            name=name,
            cache_tools_list=True,
            client_session_timeout_seconds=TIMEOUT,
            max_retry_attempts=RETRIES,
            retry_backoff_seconds_base=BACKOFF,
            tool_filter=tool_filter,
        )

    def create_streams(self) -> AbstractAsyncContextManager:
        return _captured_stdio(self.params, self)

    async def connect(self):
        if self.telemetry.circuit_is_open(self.name):
            raise CircuitOpenError(f"MCP server '{self.name}' circuit is open")
        last_error: BaseException | None = None
        for attempt in range(RETRIES + 1):
            self.telemetry.mark_starting(self.name)
            try:
                _, latency = await measured(super().connect(), STARTUP_TIMEOUT)
                # Initialization plus a bounded tool listing is the startup health check.
                await measured(super().list_tools(), TIMEOUT)
                self.telemetry.mark_success(self.name, latency)
                return self
            except Exception as exc:
                last_error = exc
                diagnostic = (
                    f"{exc}; subprocess: {self._last_diagnostic}" if self._last_diagnostic else exc
                )
                self.telemetry.mark_failure(
                    self.name,
                    diagnostic,
                    threshold=CIRCUIT_THRESHOLD,
                    reset_seconds=CIRCUIT_RESET_SECONDS,
                )
                try:
                    await self.cleanup()
                except Exception:
                    pass
                if attempt < RETRIES:
                    await asyncio.sleep(BACKOFF * (2**attempt))
        assert last_error is not None
        raise last_error

    async def list_tools(self, run_context=None, agent=None):
        started = monotonic()
        try:
            result = await super().list_tools(run_context, agent)
        except Exception as exc:
            self.telemetry.mark_failure(
                self.name,
                exc,
                threshold=CIRCUIT_THRESHOLD,
                reset_seconds=CIRCUIT_RESET_SECONDS,
            )
            raise
        self.telemetry.mark_success(self.name, (monotonic() - started) * 1000)
        return result

    async def call_tool(self, tool_name, arguments, meta=None):
        if self.telemetry.circuit_is_open(self.name):
            raise CircuitOpenError(f"MCP server '{self.name}' circuit is open")
        started = monotonic()
        try:
            result = await super().call_tool(tool_name, arguments, meta)
            if getattr(result, "isError", False):
                self.telemetry.mark_failure(
                    self.name,
                    f"tool {tool_name} returned an error",
                    threshold=CIRCUIT_THRESHOLD,
                    reset_seconds=CIRCUIT_RESET_SECONDS,
                )
            else:
                self.telemetry.mark_success(self.name, (monotonic() - started) * 1000)
            return result
        except Exception as exc:
            self.telemetry.mark_failure(
                self.name,
                exc,
                threshold=CIRCUIT_THRESHOLD,
                reset_seconds=CIRCUIT_RESET_SECONDS,
            )
            raise

    async def read_resource(self, uri):
        if self.telemetry.circuit_is_open(self.name):
            raise CircuitOpenError(f"MCP server '{self.name}' circuit is open")
        started = monotonic()
        try:
            result = await super().read_resource(uri)
        except Exception as exc:
            self.telemetry.mark_failure(
                self.name,
                exc,
                threshold=CIRCUIT_THRESHOLD,
                reset_seconds=CIRCUIT_RESET_SECONDS,
            )
            raise
        self.telemetry.mark_success(self.name, (monotonic() - started) * 1000)
        return result


def _server(params, name: str, *, tool_filter=None) -> ObservedMCPServerStdio:
    return ObservedMCPServerStdio(params, name=name, tool_filter=tool_filter)


def local_server_params(module: str, extra_env: dict[str, str] | None = None) -> dict:
    params = {"command": "uv", "args": ["run", "-m", module], "cwd": PROJECT_DIR}
    # The MCP transport intentionally inherits only a small safe environment. Preserve an
    # explicitly selected uv cache location for sandboxed/read-only home directories.
    environment = dict(extra_env or {})
    if os.getenv("UV_CACHE_DIR"):
        environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    if environment:
        params["env"] = environment
    return params


def trader_mcp_servers() -> list[ObservedMCPServerStdio]:
    """Model-facing tools cannot mutate accounts; execution follows structured output."""
    notification_env = {
        name: os.environ[name]
        for name in ("PUSHOVER_USER", "PUSHOVER_TOKEN")
        if os.getenv(name)
    }
    return [
        _server(
            local_server_params("backend.push_server", notification_env),
            "notifications",
        ),
        _server(
            local_server_params("backend.market_server"),
            "market-data",
        ),
    ]


def researcher_mcp_servers(_name: str) -> list[ObservedMCPServerStdio]:
    """Expose only the project-owned, response-bounded research search surface."""
    tavily_env = {}
    if os.getenv("TAVILY_API_KEY"):
        tavily_env["TAVILY_API_KEY"] = os.environ["TAVILY_API_KEY"]
    return [
        _server(
            local_server_params("backend.research_search_server", tavily_env),
            "research-search",
        ),
    ]


def attribute_runtime_failure(error: object) -> None:
    """Attribute SDK request failures whose safe message identifies an MCP service."""
    message = str(error).lower()
    for health in telemetry.services():
        if health.name.lower() in message:
            telemetry.mark_failure(
                health.name,
                error,
                threshold=CIRCUIT_THRESHOLD,
                reset_seconds=CIRCUIT_RESET_SECONDS,
            )
