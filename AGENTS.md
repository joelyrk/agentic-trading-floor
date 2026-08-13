# AGENTS.md

## Mission

Build a credible, auditable multi-agent paper-trading and decision-evaluation platform. The product demonstrates agent orchestration, MCP integration, deterministic controls, temporal data integrity, replayable evaluation, and operational reliability. It must not imply that synthetic results validate an investment strategy or that paper orders are real trades.

The ordered source of truth for implementation is `build-plan.md`. Work on the earliest incomplete phase unless the user explicitly changes priority.

## Repository map

- `backend/`: Python agents, MCP servers, market data, paper accounts, persistence, tracing, scheduler, and FastAPI API.
- `frontend/`: Vite and TypeScript dashboard.
- `memory/`: runtime directory for per-agent MCP memory databases; generated databases are ignored.
- `tests/`: Python tests (create and expand this as phases are implemented).
- `evals/`: replay scenarios, fixtures, graders, and result schemas (introduced by the evaluation phase).

Do not add notebooks, a second dashboard framework, copied course material, or community-contribution code. Prefer one production path through FastAPI and the Vite frontend.

## Non-negotiable product invariants

1. This is paper trading only. Never add broker execution, custody, or real-money order placement without an explicit product-scope change.
2. Every market observation must carry source, data mode, market timestamp, retrieval timestamp, and freshness. A bare `float` is not an acceptable long-term market-data contract.
3. Never silently substitute simulated data for Massive data. Degraded or simulated operation must be explicit in API responses, logs, traces, and UI.
4. Research, prices, decisions, and evaluation outcomes must obey point-in-time boundaries. Future information must never enter an agent prompt or decision-time tool response.
5. Agents may propose trades; deterministic code approves, rejects, sizes, and executes them.
6. Risk rules, cash constraints, position limits, idempotency, and accounting invariants must not depend on model judgment.
7. Persist the evidence and policy results behind every decision. Show concise decision rationale, not hidden chain-of-thought.
8. Never expose API keys in source, logs, traces, fixtures, frontend bundles, or error responses.

## Development workflow

Use Python 3.12 and the checked-in `uv.lock`:

```bash
uv sync
uv run pytest
uv run python -m compileall -q backend
```

Frontend commands:

```bash
cd frontend
npm ci
npm run build
```

Run locally from the repository root:

```bash
uv run python -m backend.trading_floor
uv run uvicorn backend.api:app --port 8000
```

In a separate terminal:

```bash
cd frontend
npm run dev
```

Use `apply_patch` for hand-authored edits. Preserve unrelated user changes. Do not commit `.env`, databases, generated frontend assets, dependency directories, caches, or trace payloads containing sensitive data.

## Architecture and coding rules

- Keep domain logic independent from agent prompts and HTTP handlers.
- Use typed Pydantic models at market-data, research, proposal, risk, execution, API, and evaluation boundaries.
- Inject clocks and data providers where time or external services affect behavior.
- Keep MCP tool surfaces narrow, typed, and purpose-specific. Do not expose Massive's generic endpoint tool directly to trading agents.
- Validate ticker symbols, quantities, timestamps, data freshness, and price modes at boundaries.
- Use UTC ISO 8601 timestamps internally. Convert only for display.
- Make database state transitions atomic. Use stable decision/order IDs to prevent duplicate execution during retries.
- Add bounded timeouts, retries with backoff, and explicit health state around external services.
- Do not suppress MCP subprocess stderr globally. Capture it safely and report the failing server by name without leaking secrets.
- Keep prompts versioned or otherwise identifiable in evaluation results.
- Prefer deterministic tests. External Massive, Tavily, and model calls must be mocked in the default test suite.
- Avoid adding dependencies unless they remove substantial complexity. Update both `pyproject.toml`/`uv.lock` or `package.json`/`package-lock.json` together.

## Testing expectations

Every behavioral change must include tests at the lowest useful level. High-risk paths require integration coverage:

- Market entitlement failures and stale data.
- No silent fallback between real and simulated modes.
- Risk-policy approval and rejection boundaries.
- Insufficient cash, overselling, duplicate orders, and database rollback.
- Replay clock and no-look-ahead enforcement.
- MCP startup failure attribution and degraded health.
- API schema compatibility with frontend types.

Before handing off a phase, run Python tests, Python compilation, and the frontend production build. Document any test that requires paid credentials; credentialed tests must remain opt-in.

## Definition of done

A phase is complete only when its acceptance criteria in `build-plan.md` are met, tests pass, configuration and schemas are documented, and the UI/API do not overstate the system's data quality or financial meaning. Update the plan's checkboxes and decision log as work lands.

