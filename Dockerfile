# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.24 AS uv
FROM python:3.12.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app \
    && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY evals ./evals
RUN mkdir -p /data && chown -R app:app /app /data
USER app

ENV ACCOUNTS_DB=/data/accounts.db \
    APP_MODE=standard \
    MARKET_DATA_MODE=simulated \
    MARKET_DATA_FALLBACK=fail_closed
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1
CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
