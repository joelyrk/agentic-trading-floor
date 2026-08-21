"""Optional public-mode authentication and bounded in-memory rate limiting."""

from __future__ import annotations

import secrets
from collections import defaultdict, deque
from time import monotonic

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.config import APIAccessSettings


class AccessControlMiddleware:
    def __init__(self, app: ASGIApp, settings: APIAccessSettings):
        self.app = app
        self.settings = settings
        self.requests: dict[str, deque[float]] = defaultdict(deque)
        self.max_tracked_clients = 10_000

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.settings.access_mode == "local":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        if client_ip not in self.requests and len(self.requests) >= self.max_tracked_clients:
            self.requests.pop(next(iter(self.requests)))
        now = monotonic()
        history = self.requests[client_ip]
        cutoff = now - self.settings.rate_limit_window_seconds
        while history and history[0] <= cutoff:
            history.popleft()
        if len(history) >= self.settings.rate_limit_requests:
            await JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.settings.rate_limit_window_seconds)},
            )(scope, receive, send)
            return
        history.append(now)
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            authorization = Headers(scope=scope).get("authorization", "")
            expected = f"Bearer {self.settings.auth_token.get_secret_value()}"
            if not secrets.compare_digest(authorization, expected):
                await JSONResponse({"detail": "authentication required"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


class ReadOnlyModeMiddleware:
    """Enforce deployment immutability before a mutating endpoint can run."""

    def __init__(
        self, app: ASGIApp, read_only: bool, detail: str = "seeded demo mode is read-only"
    ):
        self.app = app
        self.read_only = read_only
        self.detail = detail

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.read_only
            and scope["type"] == "http"
            and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        ):
            await JSONResponse(
                {"detail": self.detail},
                status_code=403,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)
