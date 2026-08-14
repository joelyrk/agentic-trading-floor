# Agentic Trading Floor — Phased Build Plan

## Objective

Turn the current course-derived demo into a portfolio-grade, evidence-grounded paper-trading and evaluation platform. The system should demonstrate that agentic decisions can be inspected and measured under controlled conditions; it should not claim profitable real-world trading.

Phases are ordered by risk and dependency. Coding agents should finish the acceptance gate for the earliest incomplete phase before starting later phases. UI polish deliberately follows data integrity, controls, and evaluation.

## Baseline at plan creation

- Four strategy agents use a researcher agent as a tool.
- MCP servers provide paper-account operations, notifications, market access, web search, fetch, and memory.
- SQLite persists accounts and activity logs.
- FastAPI exposes read-only dashboard data to a Vite frontend.
- Massive is currently exposed as a generic MCP server when a key exists; otherwise a synthetic price server is used.
- Trades are executed directly through account MCP tools without a separate risk-approval layer.
- Research evidence, price timestamps, evaluation scenarios, baselines, and service health are not yet first-class records.

---

## Phase 1 — P0: Market-data integrity and temporal correctness

**Why first:** Nothing downstream is credible until every decision uses a known, appropriately timed price. This phase removes ambiguous pricing, entitlement-driven tool choice, and silent fallback.

### Deliverables

- [x] Replace direct exposure of Massive's generic MCP server with the project-owned market MCP server in all modes.
- [x] Introduce a typed `MarketObservation` containing at least:
  - `symbol`
  - `price`
  - `currency`
  - `market_timestamp`
  - `retrieved_at`
  - `source` (`massive` or `simulator`)
  - `mode` (`end_of_day`, `delayed`, `real_time`, or `simulated`)
  - `is_stale`
  - `provider_endpoint`
- [x] Implement explicit provider adapters behind one interface: Massive EOD and deterministic simulator.
- [x] For Massive's free tier, use supported daily/previous-close endpoints intentionally; do not probe unauthorized live endpoints on each request.
- [x] Add a configured `MARKET_DATA_MODE`; fail startup if requested capabilities do not match available credentials/entitlements.
- [x] Replace silent fallback with a policy: `fail_closed`, `explicit_simulator`, or `last_known_good`. Default to `fail_closed` when Massive is configured.
- [x] Add a UTC clock abstraction and freshness policy.
- [x] Persist the exact market observation used for each valuation and proposed/executed order.
- [x] Update `/api/market` to report provider, mode, last successful observation, freshness threshold, degraded state, and error summary.
- [x] Display an unmistakable EOD/delayed/real-time/simulated badge in the frontend.

### Suggested code shape

```text
backend/market/
  models.py
  provider.py
  massive.py
  simulator.py
  service.py
  server.py
```

Migrate incrementally; do not delete the old path until callers use the typed service.

### Tests

- [x] Massive success, authentication failure, entitlement failure, timeout, malformed payload, and empty market day.
- [x] Weekend/holiday previous-close behavior.
- [x] Freshness boundaries with an injected clock.
- [x] Explicit simulator selection and prohibition of silent Massive-to-simulator fallback.
- [x] MCP tool schema and full stdio handshake.

### Acceptance gate

Given any portfolio value or order, a reviewer can identify the exact price, source, data mode, and timestamp used. If Massive fails, the system either stops the cycle or visibly follows the configured fallback policy; it never presents synthetic data as market data.

---

## Phase 2 — P0: Structured decisions and deterministic risk/execution

**Why next:** Model output must not directly mutate an account without enforceable policy and replay-safe execution.

### Deliverables

- [x] Define typed `ResearchBrief`, `TradeProposal`, `RiskDecision`, `PaperOrder`, and `ExecutionResult` schemas.
- [x] Make trader agents return structured proposals instead of directly calling buy/sell account tools.
- [x] Add a deterministic risk engine with configurable rules:
  - maximum position percentage
  - maximum symbol and sector concentration
  - minimum cash reserve
  - maximum order notional and daily turnover
  - maximum drawdown / kill switch
  - stale or incompatible market-data rejection
  - allowed universe and positive integral quantity validation
- [x] Separate proposal, approval, and execution services.
- [x] Assign stable decision and order IDs; make execution idempotent.
- [x] Wrap balance, holdings, observation, and transaction writes in one atomic database transaction.
- [x] Record every approval and rejection with rule-level reasons.
- [x] Add human approval as a configurable policy for high-risk proposals, disabled by default in automated replay.

### Tests

- [x] Boundary/property tests for every risk rule.
- [x] Insufficient cash, overselling, duplicate IDs, concurrent attempts, and rollback after failure.
- [x] Agent output validation and safe rejection of malformed proposals.
- [x] No execution occurs without a persisted approval and market observation.

### Acceptance gate

No model-controlled path can bypass risk validation or execute the same order twice. All account mutations preserve cash/holdings invariants and have an auditable proposal-to-execution chain.

---

## Phase 3 — P0: Auditable, point-in-time research

**Why now:** Price integrity and controlled execution make it worthwhile to improve evidence quality. Research must be verifiable and temporally aligned with decisions.

### Deliverables

- [x] Define structured `SourceRecord`, `EvidenceClaim`, and `ResearchBrief` models.
- [x] Store canonical URL, publisher, title, publication time, retrieval time, claim, short supporting excerpt/hash, stance, confidence, and caveats.
- [x] Require each material recommendation claim to reference one or more source IDs.
- [x] Add source deduplication, domain allow/deny policy, and publication-time checks.
- [x] Reject or flag sources published after the decision cutoff.
- [x] Version researcher/trader prompts and store those versions with each run.
- [x] Create an evidence API and frontend drill-down from recommendation to sources, market observation, risk decision, and execution.
- [x] Present concise rationale and evidence only; never expose private chain-of-thought.

### Tests

- [x] Missing citations, broken citations, duplicate articles, conflicting publication dates, future-dated sources, and unsupported claims.
- [x] Research brief schema stability and database round-trip.

### Acceptance gate

A reviewer can open any decision and verify which timestamped evidence supported it, which claims lacked support, and whether all information existed before the decision cutoff.

---

## Phase 4 — P1: Replayable evaluation and baselines

**Why here:** Evaluation becomes meaningful only after inputs, decisions, and execution are temporally controlled and persisted.

### Deliverables

- [x] Add `evals/` with versioned scenario manifests, immutable fixtures, schemas, and runners.
- [x] Introduce a simulation clock used by research cutoff, market data, decisions, and execution.
- [x] Build 30–100 representative historical scenarios without future data in agent context.
- [x] Separate decision-time fixtures from outcome data; make look-ahead leakage structurally difficult.
- [x] Add baselines: buy-and-hold, equal weight, no trade, random valid trades, and a simple momentum rule.
- [x] Add an ablation comparing multi-agent and single-agent workflows.
- [x] Measure total/benchmark-relative return, volatility, Sharpe, max drawdown, turnover, win rate, decision validity, citation validity, tool success, latency, and model/API cost.
- [x] Store run metadata: dataset version, git SHA, model, prompt version, configuration, seed, and timestamps.
- [x] Generate machine-readable JSON and a concise Markdown evaluation report.

### Tests

- [x] Replay determinism for non-model components.
- [x] No-look-ahead assertions across sources and prices.
- [x] Metric calculations against known fixtures.
- [x] Resume/retry without duplicate orders.

### Acceptance gate

One documented command reproduces an evaluation report comparing the agent system with simple baselines, and every scenario proves that only contemporaneously available inputs reached the decision.

---

## Phase 5 — P1: Observability, health, and cost controls

### Deliverables

- [x] Give every MCP server a stable name and startup health check.
- [x] Capture subprocess diagnostics safely; report which server failed and why without secrets.
- [x] Add bounded retry/backoff, timeouts, circuit breakers, and degraded-state transitions.
- [x] Expose `/api/health` with per-server state, last success/error, latency, data freshness, and current cycle ID.
- [x] Add trace metadata for decision ID, scenario/run ID, prompt version, market mode, and model.
- [x] Disable sensitive trace payload capture where credentials or proprietary data could appear.
- [x] Record request/token usage, estimated cost, latency, MCP failure rate, and cycle success rate.
- [x] Add budgets for turns, tokens, wall time, and spend per cycle.

### Acceptance gate

A failed service is attributable within one dashboard/API view, cycles fail or degrade according to policy, and each completed decision has trace, latency, and cost metadata.

---

## Phase 6 — P1: Portfolio-grade product experience

### Deliverables

- [ ] Replace decorative-first views with decision transparency:
  - portfolio versus benchmark
  - drawdown and turnover
  - data mode/freshness
  - evidence and recommendation timeline
  - approved/rejected proposals
  - risk utilization
  - service health
  - run cost and latency
- [ ] Add a replay screen to select a scenario, execute it, inspect decisions, and reveal outcomes only after decision completion.
- [ ] Add experiment comparison across models, prompts, and agent architectures.
- [ ] Ensure accessible color contrast, keyboard navigation, empty/error/loading states, and responsive layouts.
- [ ] Generate API types from a versioned OpenAPI schema or add a contract test preventing backend/frontend drift.

### Acceptance gate

A reviewer can understand data quality, evidence, controls, system health, and benchmark-relative results without reading source code.

---

## Phase 7 — P2: Reliability, security, and CI

### Deliverables

- [ ] Add unit, integration, API contract, and frontend tests to CI.
- [ ] Add formatting, linting, type checking, secret scanning, and dependency auditing.
- [ ] Validate configuration at startup with actionable errors.
- [ ] Add database migrations, backup/restore guidance, retention rules, and corruption recovery.
- [ ] Add rate limits and authentication if the API becomes publicly writable.
- [ ] Threat-model prompt injection, malicious web content, tool abuse, SSRF, MCP supply-chain risk, and trace leakage.
- [ ] Pin or verify external MCP packages; replace git-based runtime installs with controlled versions where practical.
- [ ] Add graceful shutdown and recovery tests for interrupted cycles.

### Acceptance gate

CI validates the main workflows without external credentials, security assumptions are documented, and restart/retry behavior cannot corrupt or duplicate portfolio state.

---

## Phase 8 — P2: Reproducible deployment and portfolio packaging

### Deliverables

- [ ] Add production containers and a local orchestration file for API, scheduler, and frontend.
- [ ] Add a seeded read-only demo mode that needs no paid credentials.
- [ ] Document local setup, architecture, data contracts, evaluation method, limitations, security, and operating modes.
- [ ] Add `ARCHITECTURE.md`, `EVALUATION.md`, and `SECURITY.md` backed by implemented behavior.
- [ ] Include an architecture diagram, screenshots, example traces with sensitive data removed, and a short demo video.
- [ ] Publish evaluation results with dataset/model/prompt versions and honest limitations.
- [ ] Deploy with server-side secrets only; never send provider keys to the browser.

### Acceptance gate

A reviewer can clone and run the safe demo, understand the architecture in minutes, reproduce the published evaluation, and see clearly bounded claims about what the system does and does not prove.

---

## Cross-phase quality gates

Run before completing every phase:

```bash
uv run pytest
uv run python -m compileall -q backend
cd frontend && npm run build
```

Also verify:

- [ ] `.env`, database files, generated assets, and sensitive traces are not tracked.
- [ ] API/schema changes are reflected in frontend types and documentation.
- [ ] New external calls have mocked default tests and explicit timeouts.
- [ ] Claims in the UI/README match the actual market-data mode and evaluation evidence.
- [ ] `AGENTS.md` remains consistent with the implemented architecture.

## Decision log

Record material architecture decisions here as short dated entries or replace this section with ADR files when it grows.

- **2026-08-13:** Standalone repository uses FastAPI plus Vite as the sole product UI path; course notebooks and the redundant Gradio dashboard were removed.
- **2026-08-13:** Python is pinned to 3.12. Trading agents use only the project-owned typed market MCP server; Massive's generic MCP package is no longer exposed or launched.
- **2026-08-13:** Market-data integrity and point-in-time correctness precede strategy claims, UI expansion, and deployment.
- **2026-08-13:** Phase 1 supports Massive previous-close EOD and deterministic simulation. Fail-closed is the default; simulator fallback must be explicit and is always surfaced as degraded simulated data.
- **2026-08-13:** Trader agents return structured proposals and have no account-mutation MCP tools. Deterministic services persist proposal observations, evaluate configurable rule-level risk policy, and execute approved paper orders atomically with stable idempotency keys.
- **2026-08-13:** Sector concentration uses configured classifications rather than model claims; unmapped symbols share a conservative `unclassified` sector. High-risk human approval is opt-in and disabled during automated replay.
- **2026-08-13:** Phase 3 uses a versioned, citation-linked research graph persisted with each run in SQLite. Canonical URL/content deduplication, domain policy, and publication cutoffs are deterministic gates; prompt versions and concise evidence are persisted without chain-of-thought.
- **2026-08-13:** Phase 4 uses hashed, physically separated decision/outcome fixtures and an injected monotonic simulation clock. The credential-free default evaluator compares five deterministic baselines plus single-agent/multi-agent workflow proxies, checkpoints by stable scenario keys and order IDs, and emits versioned JSON/Markdown reports. Its 30-scenario historical fixture and derived benchmark proxy are explicitly limited to replay-system validation, not investment-strategy claims.
- **2026-08-14:** Phase 5 supervises stable logical MCP services with bounded startup probes, request retries, redacted stderr diagnostics, persisted circuit state, and explicit healthy/degraded/unavailable transitions. Per-trader cycles enforce turn/token/wall-time/spend budgets, disable sensitive trace payload capture, and persist provider-reported usage, configurable cost estimates, latency, trace/run/prompt/market metadata, and decision links for the health API and dashboard.
