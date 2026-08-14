"""Actionable startup validation for API and scheduler processes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator


class APIAccessSettings(BaseModel):
    access_mode: Literal["local", "public"] = "local"
    auth_token: SecretStr | None = None
    rate_limit_requests: int = Field(default=60, gt=0, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, gt=0, le=3_600)

    @model_validator(mode="after")
    def require_public_controls(self):
        token = self.auth_token.get_secret_value() if self.auth_token else ""
        if self.access_mode == "public" and len(token) < 32:
            raise ValueError(
                "API_ACCESS_MODE=public requires API_AUTH_TOKEN with at least 32 characters"
            )
        return self

    @classmethod
    def from_env(cls) -> "APIAccessSettings":
        token = os.getenv("API_AUTH_TOKEN", "").strip()
        return cls(
            access_mode=os.getenv("API_ACCESS_MODE", "local").strip().lower(),
            auth_token=token or None,
            rate_limit_requests=os.getenv("API_RATE_LIMIT_REQUESTS", "60"),
            rate_limit_window_seconds=os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"),
        )


class RuntimeSettings(BaseModel):
    scheduler_interval_minutes: int = Field(gt=0, le=1_440)
    mcp_startup_timeout_seconds: float = Field(gt=0, le=120)
    mcp_request_timeout_seconds: float = Field(gt=0, le=300)
    mcp_max_retries: int = Field(ge=0, le=10)
    mcp_retry_backoff_seconds: float = Field(ge=0, le=30)
    mcp_circuit_failure_threshold: int = Field(gt=0, le=100)
    mcp_circuit_reset_seconds: float = Field(gt=0, le=86_400)
    shutdown_grace_seconds: float = Field(gt=0, le=300)
    accounts_db: Path

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        return cls(
            scheduler_interval_minutes=os.getenv("RUN_EVERY_N_MINUTES", "60"),
            mcp_startup_timeout_seconds=os.getenv("MCP_STARTUP_TIMEOUT_SECONDS", "20"),
            mcp_request_timeout_seconds=os.getenv("MCP_REQUEST_TIMEOUT_SECONDS", "30"),
            mcp_max_retries=os.getenv("MCP_MAX_RETRIES", "2"),
            mcp_retry_backoff_seconds=os.getenv("MCP_RETRY_BACKOFF_SECONDS", "0.5"),
            mcp_circuit_failure_threshold=os.getenv("MCP_CIRCUIT_FAILURE_THRESHOLD", "3"),
            mcp_circuit_reset_seconds=os.getenv("MCP_CIRCUIT_RESET_SECONDS", "60"),
            shutdown_grace_seconds=os.getenv("SHUTDOWN_GRACE_SECONDS", "30"),
            accounts_db=Path(os.getenv("ACCOUNTS_DB", "accounts.db")),
        )


def validate_startup(component: Literal["api", "scheduler"]) -> RuntimeSettings:
    """Validate all deterministic configuration before starting a process."""
    runtime = RuntimeSettings.from_env()
    APIAccessSettings.from_env()
    if runtime.accounts_db.exists() and runtime.accounts_db.is_dir():
        raise ValueError(f"ACCOUNTS_DB points to a directory: {runtime.accounts_db}")
    # Lazy imports keep path validation ahead of any SQLite initialization.
    from backend.decisions.config import RiskPolicy
    from backend.market.config import MarketSettings
    from backend.observability import CycleBudget

    MarketSettings.from_env()
    RiskPolicy.from_env()
    CycleBudget.from_env()
    if component == "scheduler":
        model = os.getenv("MODEL_NAME", "gpt-5.4-mini")
        use_many = os.getenv("USE_MANY_MODELS", "false").strip().lower() == "true"
        missing: list[str] = []
        if not os.getenv("OPENAI_API_KEY") and not use_many and "/" not in model:
            missing.append("OPENAI_API_KEY")
        if not os.getenv("TAVILY_API_KEY"):
            missing.append("TAVILY_API_KEY")
        if use_many:
            for key in (
                "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY",
                "GOOGLE_API_KEY",
                "GROK_API_KEY",
            ):
                if not os.getenv(key):
                    missing.append(key)
        if missing:
            raise ValueError(
                "scheduler configuration is missing required credentials: "
                + ", ".join(sorted(set(missing)))
            )
    return runtime
