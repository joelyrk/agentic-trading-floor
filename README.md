# Agentic Trading Floor

A multi-agent paper-trading application built with the OpenAI Agents SDK and Model Context Protocol (MCP). Four strategy agents share research, request market data, operate paper accounts, and expose their activity through a FastAPI backend and Vite frontend.

This is an educational simulation. It does not place real orders and is not financial advice.

## Architecture

- `backend/`: agents, MCP servers, paper accounts, market data, tracing, and API
- `frontend/`: Vite/TypeScript dashboard
- `memory/`: runtime location for per-agent MCP memory databases
- `build-plan.md`: priority-ordered roadmap and acceptance gates

All agents use the project-owned typed market MCP server. Behind it, the configured provider is either Massive's previous-close endpoint (`end_of_day`) or the deterministic simulator (`simulated`). The application never probes progressively more privileged Massive endpoints and never silently changes to synthetic prices.

Trader agents cannot mutate accounts. They return validated `TradingDecision` output containing a research brief and zero or more proposals. Deterministic services fetch and persist the exact observation, evaluate every risk rule, and only execute a persisted approval. Paper execution uses stable IDs and a single SQLite transaction for cash, holdings, the transaction record, its observation, the order, result, and audit log.

Research is a versioned, point-in-time contract rather than free-form analysis. Each material recommendation cites a structured claim; each claim links to canonicalized source records containing publisher, title, publication/retrieval times, a bounded supporting excerpt and SHA-256 hash, confidence, stance, and caveats. Future publications, broken citations, duplicate URLs/content, conflicting dates, and disallowed domains are rejected before a proposal can reach risk review. Only concise evidence and rationale are stored—private chain-of-thought is neither requested nor exposed.

## Requirements

- Python 3.12
- `uv`
- Node.js 22+
- npm
- `npx` (used by the Tavily and memory MCP servers)

## Setup

```bash
cp .env.example .env
uv sync
cd frontend
npm ci
```

Add at least `OPENAI_API_KEY` and `TAVILY_API_KEY` to `.env`. Market data is configured explicitly:

```dotenv
# Safe credential-free demo
MARKET_DATA_MODE=simulated
MARKET_DATA_FALLBACK=fail_closed

# Or Massive's end-of-day previous close
MARKET_DATA_MODE=end_of_day
MASSIVE_API_KEY=...
MARKET_DATA_FALLBACK=fail_closed
```

`MARKET_DATA_FALLBACK` accepts `fail_closed`, `explicit_simulator`, or `last_known_good`. `fail_closed` is the default, including whenever Massive is configured. An explicit simulator fallback is returned with `source=simulator`, `mode=simulated`, and degraded health; it is never presented as Massive data. `last_known_good` is process-local and is re-evaluated against the freshness threshold before every use.

`MARKET_DATA_FRESHNESS_SECONDS` defaults to 300 seconds for simulated observations and four days for EOD observations so a Friday close remains usable over an ordinary weekend. `MARKET_DATA_TIMEOUT_SECONDS` defaults to 10 seconds. This phase supports only `simulated` and `end_of_day`; requesting `delayed` or `real_time`, or requesting EOD without a key, fails startup with an actionable configuration error.

Risk policy is configured with these optional environment variables:

```dotenv
RISK_MAX_POSITION_PERCENTAGE=0.30
RISK_MAX_SYMBOL_CONCENTRATION=0.30
RISK_MAX_SECTOR_CONCENTRATION=0.50
RISK_MINIMUM_CASH_RESERVE=500
RISK_MAXIMUM_ORDER_NOTIONAL=2500
RISK_MAXIMUM_DAILY_TURNOVER=5000
RISK_MAXIMUM_DRAWDOWN=0.25
RISK_ALLOWED_UNIVERSE=AAPL,MSFT,NVDA
RISK_ALLOWED_MARKET_MODES=end_of_day,simulated
RISK_SECTOR_MAP={"AAPL":"technology","MSFT":"technology","NVDA":"technology"}
RISK_HUMAN_APPROVAL_ENABLED=false
RISK_HUMAN_APPROVAL_NOTIONAL=2000
AUTOMATED_REPLAY=false
```

An empty allowed universe permits any syntactically valid ticker. Sector classification never trusts the model: configured mappings are authoritative and unmapped symbols share the conservative `unclassified` bucket. Human approval is off by default and is bypassed only when `AUTOMATED_REPLAY=true`; that replay setting does not bypass any deterministic risk rule.

Research source policy is optional and deterministic:

```dotenv
# Empty means all valid HTTP(S) domains are allowed.
RESEARCH_ALLOWED_DOMAINS=reuters.com,sec.gov
# Deny rules take precedence and include subdomains.
RESEARCH_DENIED_DOMAINS=example.invalid
```

The research schema is version `1.0`; live agent runs persist `researcher-v1` and `trader-v1` prompt versions with the research record. The decision cutoff is injected into both prompts. Source publication times must be at or before that cutoff, and source retrieval must precede proposal creation.

## Run

Open three terminals in the project root.

Trading scheduler:

```bash
uv run python -m backend.trading_floor
```

Read-only API:

```bash
uv run uvicorn backend.api:app --port 8000
```

Frontend:

```bash
cd frontend
npm run dev
```

Vite normally serves the dashboard at `http://localhost:5173`.

## Market-data contract

Every observation includes `symbol`, `price`, `currency`, `market_timestamp`, `retrieved_at`, `source`, `mode`, `is_stale`, and `provider_endpoint`. Timestamps are timezone-aware UTC values, and future-dated market observations are rejected. The exact observation behind each holding valuation is returned by the trader API and stored in `market_observations`; each new paper transaction also embeds and references its execution observation.

`GET /api/market` reports the effective and configured provider/mode, fallback policy, last successful observation, freshness threshold, degraded state, and a credential-safe error summary. The dashboard labels EOD, delayed, real-time, and simulated modes explicitly. These labels describe data quality, not whether the paper-trading scheduler is placing real trades—it never does.

`GET /api/traders/{name}/decisions` returns the complete proposal, rule-level risk decision, paper order, and execution chain. When human approval policy is active, `POST /api/decisions/{decision_id}/approve` records explicit approval and submits that approved paper order idempotently. This endpoint only affects the local paper account; it has no broker integration.

`GET /api/evidence/{proposal_id}` returns the citation-linked research brief and prompt versions together with the exact market observation, rule-level risk decision, paper order, and execution result. The dashboard’s recommendation drill-down consumes this endpoint and links only to canonical source URLs; it shows concise evidence and never model chain-of-thought.

Massive credentialed tests are not part of the default suite. Provider behavior is tested with deterministic fakes for success, authentication failure, entitlement failure, timeout, malformed responses, empty market days, and weekend previous-close handling.
