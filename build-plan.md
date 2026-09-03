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

- [x] Replace decorative-first views with decision transparency:
  - portfolio versus benchmark
  - drawdown and turnover
  - data mode/freshness
  - evidence and recommendation timeline
  - approved/rejected proposals
  - risk utilization
  - service health
  - run cost and latency
- [x] Add a replay screen to select a scenario, execute it, inspect decisions, and reveal outcomes only after decision completion.
- [x] Add experiment comparison across models, prompts, and agent architectures.
- [x] Ensure accessible color contrast, keyboard navigation, empty/error/loading states, and responsive layouts.
- [x] Generate API types from a versioned OpenAPI schema or add a contract test preventing backend/frontend drift.

### Acceptance gate

A reviewer can understand data quality, evidence, controls, system health, and benchmark-relative results without reading source code.

---

## Phase 7 — P2: Reliability, security, and CI

### Deliverables

- [x] Add unit, integration, API contract, and frontend tests to CI.
- [x] Add formatting, linting, type checking, secret scanning, and dependency auditing.
- [x] Validate configuration at startup with actionable errors.
- [x] Add database migrations, backup/restore guidance, retention rules, and corruption recovery.
- [x] Add rate limits and authentication if the API becomes publicly writable.
- [x] Threat-model prompt injection, malicious web content, tool abuse, SSRF, MCP supply-chain risk, and trace leakage.
- [x] Pin or verify external MCP packages; replace git-based runtime installs with controlled versions where practical.
- [x] Add graceful shutdown and recovery tests for interrupted cycles.

### Acceptance gate

CI validates the main workflows without external credentials, security assumptions are documented, and restart/retry behavior cannot corrupt or duplicate portfolio state.

---

## Phase 8 — P2: Reproducible deployment and portfolio packaging

### Deliverables

- [x] Add production containers and a local orchestration file for API, scheduler, and frontend.
- [x] Add a seeded read-only demo mode that needs no paid credentials.
- [x] Document local setup, architecture, data contracts, evaluation method, limitations, security, and operating modes.
- [x] Add `ARCHITECTURE.md`, `EVALUATION.md`, and `SECURITY.md` backed by implemented behavior.
- [x] Include an architecture diagram.
- [x] Include screenshots captured from the real live and seeded Compose deployments.
- [x] Include example traces with sensitive data removed.
- [ ] Include a short demo video.
- [x] Publish evaluation results with dataset/model/prompt versions and honest limitations.
- [x] Deploy with server-side secrets only; never send provider keys to the browser.

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

- [x] `.env`, database files, generated assets, and sensitive traces are not tracked.
- [x] API/schema changes are reflected in frontend types and documentation.
- [x] New external calls have mocked default tests and explicit timeouts.
- [x] Claims in the UI/README match the actual market-data mode and evaluation evidence.
- [x] `AGENTS.md` remains consistent with the implemented architecture.

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
- **2026-08-14:** Phase 6 replaces the quadrant-first demo with an accessible decision console centered on portfolio/risk metrics, evidence and control outcomes, data provenance, service health, and cost/latency. Replay sessions persist a decision before allowing a separate idempotent outcome reveal; experiment reports compare model/prompt labels and deterministic single/multi-agent architecture proxies. FastAPI's versioned OpenAPI document is guarded by a frontend route-contract test.
- **2026-08-14:** Phase 7 makes SQLite evolution transactional and versioned, adds verified backup/restore and bounded retention tools, and records orphaned or shutdown-cancelled cycles as interrupted. Public API mode is opt-in and requires Bearer authentication plus per-client rate limits. The generic fetch MCP is replaced by a bounded public-network-only server; remaining npm MCP packages are exact-pinned. CI enforces tests, formatting, linting, TypeScript checking, secret scanning, builds, and locked dependency audits, with the threat model and recovery procedures recorded in `SECURITY.md`.
- **2026-08-14:** Phase 8 packages separate backend/frontend production images and a loopback-only Compose demo. `APP_MODE=demo` requires simulated data, seeds a versioned audit snapshot atomically, rejects HTTP mutations and scheduler startup, and exposes the boundary to the UI. Credentialed scheduler services remain an explicit profile with server-only environment secrets. A versioned offline report, architecture/evaluation/security docs, and a sanitized trace are checked in; real screenshots/video remain pending because the required browser capture runtime was unavailable during implementation.
- **2026-08-14:** The Massive EOD runtime schedules one post-close cycle at 22:30 UTC on weekdays. Traders are sequential by default, cycles are bounded to 8 turns/40k tokens/180 seconds, research output is capped at five concise sources, and OpenAI requests use bounded SDK retries that respect eligible server retry guidance. These controls prevent four-agent request bursts against data that changes only once per trading day.
- **2026-08-15:** Standard mode adds a confirmed manual EOD-cycle control backed by the same bounded sequential orchestration as the scheduler. Manual and scheduled requests share a durable SQLite reservation, cross-process active-run lock, request idempotency, and consumed-market-snapshot guard; the UI polls the audit state while demo mode and existing write authentication preserve deployment boundaries.
- **2026-08-15:** Live-cycle reliability removes generic npm research and memory MCPs from the decision-critical path. A project-owned Tavily adapter hard-caps result count, snippets, bytes, timeout, retries, and disables raw content; deterministic code owns source metadata while smaller model schemas own only synthesis and recommendations. Research failure is terminal for that agent. Run-correlated stage logs and cycle telemetry now power a per-agent live activity dashboard without exposing prompts or chain-of-thought.
- **2026-08-21:** Risk evaluation deterministically reduces oversized proposals to the largest policy-compliant whole-share quantity while preserving requested and approved quantities as separate audit fields. Execution is bound to the persisted approved size, oversells and non-quantity policy failures remain rejections, and rule explanations use outcome-accurate comparison wording.
- **2026-08-21:** Public showcase mode separates HTTP permissions from scheduler capability: standard-mode credentialed AI continues on its daily schedule while every public mutation is rejected and the UI accurately labels live AI as view-only. The live Compose services retain one shared SQLite volume, with a Caddy HTTPS example and a VPS runbook covering persistence, verification, backup, and safe updates.
- **2026-08-22:** Pushover credentials are explicitly allowlisted into the isolated notification MCP subprocess rather than being lost at the stdio environment boundary. Missing credentials and rejected provider requests now produce tool failures and degraded service telemetry instead of false healthy delivery signals; notification content is no longer printed to the MCP transport.
- **2026-08-22:** Agent desks present terminal failures as concise operational summaries, with sanitized raw diagnostics retained behind a native collapsed disclosure. This preserves audit access without allowing provider or validation dumps to dominate the dashboard layout.
- **2026-08-22:** Notifications are emitted by deterministic run-finalization code rather than model tool calls. Persisted decision outcomes and run summaries enter an idempotent SQLite outbox, are delivered through one credential-isolated notification MCP session with bounded retries, and retain auditable sent or failed state without changing the paper-run outcome.
- **2026-08-22:** Research and trader stages receive one budget-accounted repair attempt after malformed structured output. Trader prompts expose only material, cited claim IDs; deterministic processing rejects invalid evidence references and unavailable market data per proposal, preserving other valid paper decisions while retaining warnings in run telemetry.
- **2026-08-25:** App-owned paper-account and market-data MCP subprocesses receive an explicit allowlist of the configured database and market environment. This prevents split-brain account state and silent mode/price divergence while keeping unrelated process secrets out of child environments.
- **2026-09-01:** Tool-free OpenAI research synthesis uses Chat Completions structured output while tool-using traders retain Responses. Repeated incomplete-output failures remain bounded to one repair and now persist credential-safe provider response/request IDs, output item types, per-response token totals, and raw-usage availability for attributable debugging.
- **2026-09-01:** OpenAI Chat Completions research limits output with `max_completion_tokens`; compatible third-party transports retain `max_tokens`. A regression test protects the provider-specific request setting after `gpt-5.4-mini` rejected the legacy parameter before inference.
- **2026-09-03:** Research synthesis explicitly uses `none` reasoning, low verbosity, schema-bounded caveats, and retains its 8k completion ceiling. Historical successful runs stayed far below that ceiling, so cost containment remains unchanged while the targeted controls address runaway generation. Provider-side output exhaustion is terminal rather than retried, is labeled `output_limit_exceeded` with a redacted request ID and active settings, and records the completed request with unavailable token usage.
- **2026-09-03:** Superseding the prior research transport experiment, the research stage restored the August 25 contract: Responses API, a 2,500-token output ceiling, and at most two model turns. A single-agent smoke run over a fixed five-source synthetic catalog received two successful HTTP responses but no valid final structured message, ending at the two-turn limit. This reproduces the incomplete-output behavior without search, trading, private account data, or an expanded output allowance.
- **2026-09-03:** The August `ResearchSynthesis` caveat schema was also restored by removing the later per-string `maxLength` constraints. A second and final single-agent smoke run over the same fixed synthetic catalog again received two successful Responses API calls but no valid structured message, ending at the two-turn limit. The schema change is therefore not the cause of the regression.
- **2026-09-03:** Research synthesis is isolated onto pinned `gpt-4.1-mini-2025-04-14` through Chat Completions with `max_completion_tokens=2500` and one model turn per attempt. The legacy `max_tokens` parameter produced an immediate length finish with zero completion tokens on the pinned model. Tool-using trading decisions retain their configured models and the Responses API. Traces and cycle telemetry identify both models, and configured cost rates conservatively use the highest selected rate.
- **2026-09-03:** The provider-facing research output contract uses a simple all-required JSON schema without regex, length, item-count, or numeric-bound keywords. The strict `ResearchSynthesis` domain model validates those constraints deterministically after parsing and before research policy or trading, avoiding provider constrained-decoder failures without weakening application controls.
- **2026-09-03:** A production-adapter smoke run over the fixed five-source synthetic catalog verified the research fix in one request: pinned `gpt-4.1-mini-2025-04-14` returned a valid five-claim synthesis using 1,082 input and 449 output tokens (1,531 total). No search, trading, private account data, or database mutation was involved.
- **2026-09-03:** Future cycle costs price research and trading usage separately before persisting one combined `estimated_cost_usd`. Research rates are independently configurable and conservatively inherit trader rates when omitted; historical aggregate rows remain unchanged because they do not contain per-stage token splits.
