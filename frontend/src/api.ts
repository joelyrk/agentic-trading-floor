// Client for the trading floor HTTP API. All paths are relative; in dev the Vite
// proxy forwards /api to the FastAPI backend, so the browser sees one origin.

export interface TraderInfo {
  name: string;
  lastname: string;
  model_name: string;
}

export interface RuntimeInfo {
  mode: "standard" | "demo";
  read_only: boolean;
  public_showcase: boolean;
  scheduled_ai_enabled: boolean;
  paper_trading_only: true;
  credentials_required: boolean | null;
}

export interface Holding {
  symbol: string;
  quantity: number;
  price: number;
  avg_cost: number;
  market_value: number;
  unrealized_pnl: number;
  market_observation_id: string;
  market_observation: MarketObservation;
}

export interface Transaction {
  symbol: string;
  quantity: number;
  price: number;
  timestamp: string;
  rationale: string;
  market_observation_id?: string;
  market_observation?: MarketObservation;
}

export interface TimePoint {
  datetime: string;
  value: number;
}

// Mirrors the full backend payload; the dashboard renders a subset of these fields.
export interface TraderDetail extends TraderInfo {
  balance: number;
  strategy: string;
  portfolio_value: number;
  pnl: number;
  holdings: Holding[];
  valuation_status: {
    state: "healthy" | "degraded";
    used_persisted_observations: string[];
    error_summary: string | null;
  };
  transactions: Transaction[];
  time_series: TimePoint[];
}

export interface LogRow {
  datetime: string;
  type: string;
  message: string;
  color: string;
}

export type MarketSource = "massive" | "simulator";
export type MarketMode = "end_of_day" | "delayed" | "real_time" | "simulated";

export interface MarketObservation {
  symbol: string;
  price: string;
  currency: string;
  market_timestamp: string;
  retrieved_at: string;
  source: MarketSource;
  mode: MarketMode;
  is_stale: boolean;
  provider_endpoint: string;
}

export interface MarketInfo {
  provider: MarketSource;
  mode: MarketMode;
  configured_provider: MarketSource;
  configured_mode: MarketMode;
  fallback_policy: "fail_closed" | "explicit_simulator" | "last_known_good";
  is_market_open: boolean;
  last_successful_observation: MarketObservation | null;
  freshness_threshold_seconds: number;
  degraded: boolean;
  error_summary: string | null;
}

export interface ServiceHealth {
  name: string;
  state: "starting" | "healthy" | "degraded" | "unavailable";
  required: boolean;
  last_success: string | null;
  last_error: string | null;
  error_summary: string | null;
  latency_ms: number | null;
  consecutive_failures: number;
  circuit_open_until: string | null;
  attempt_count: number;
  failure_count: number;
}

export interface HealthInfo {
  status: "healthy" | "degraded";
  current_cycle_id: string | null;
  services: ServiceHealth[];
  metrics: {
    request_count: number;
    token_count: number;
    estimated_cost_usd: number;
    average_cycle_latency_ms: number;
    mcp_failure_rate: number;
    cycle_success_rate: number;
  };
}

export interface AgentRunRecord {
  run_id: string;
  trigger: "scheduled" | "manual";
  status: "queued" | "running" | "succeeded" | "partial_success" | "failed" | "interrupted";
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  requested_by: string;
  idempotency_key: string;
  market_symbol: string;
  market_timestamp: string;
  market_retrieved_at: string;
  market_mode: MarketMode;
  error_summary: string | null;
  retry_of: string | null;
}

export interface AgentActivity {
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "interrupted";
  started_at: string | null;
  completed_at: string | null;
  requests: number;
  total_tokens: number;
  usage_status: "available" | "unavailable";
  latency_ms: number | null;
  error_summary: string | null;
  current_activity: string;
  logs: Array<Pick<LogRow, "datetime" | "type" | "message">>;
}

export interface AgentRunProgress {
  run: AgentRunRecord;
  agents: AgentActivity[];
  can_retry: boolean;
  retry_block_reason: string | null;
}

export interface RiskPolicyInfo {
  max_position_percentage: string;
  max_symbol_concentration: string;
  max_sector_concentration: string;
  minimum_cash_reserve: string;
  maximum_order_notional: string;
  maximum_daily_turnover: string;
  maximum_drawdown: string;
  human_approval_enabled: boolean;
}

export type EvidenceStance = "supports" | "opposes" | "mixed" | "context";

export interface SourceRecord {
  source_id: string;
  canonical_url: string;
  publisher: string;
  title: string;
  published_at: string;
  retrieved_at: string;
  supporting_excerpt: string;
  content_hash: string;
  caveats: string[];
}

export interface EvidenceClaim {
  claim_id: string;
  claim: string;
  source_ids: string[];
  stance: EvidenceStance;
  confidence: string;
  material: boolean;
  caveats: string[];
}

export interface ResearchBrief {
  schema_version: "1.0";
  research_id: string;
  summary: string;
  as_of: string;
  sources: SourceRecord[];
  claims: EvidenceClaim[];
  caveats: string[];
  researcher_prompt_version: string;
}

export interface TradeProposal {
  proposal_id: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  rationale: string;
  evidence_claim_ids: string[];
  created_at: string;
  research: ResearchBrief;
  market_observation: MarketObservation;
}

export interface RiskDecisionAudit {
  outcome: "approved" | "rejected" | "pending_human";
  requested_quantity?: number | null;
  approved_quantity?: number | null;
  rules: Array<{ rule: string; passed: boolean; reason: string }>;
}

export interface DecisionAudit {
  proposal: TradeProposal;
  risk_decision: RiskDecisionAudit | null;
  order: unknown | null;
  execution: { status: string } | null;
}

export interface EvidenceChain {
  research: ResearchBrief;
  prompt_versions: { researcher: string; trader: string };
  proposal: TradeProposal;
  market_observation: MarketObservation;
  risk_decision: RiskDecisionAudit | null;
  order: unknown | null;
  execution: { status: string; executed_at: string } | null;
  telemetry: {
    cycle_id: string;
    trace_id: string;
    model: string;
    prompt_version: string;
    market_mode: MarketMode;
    total_tokens: number;
    estimated_cost_usd: string;
    latency_ms: number;
    status: string;
  } | null;
}

async function get<T>(path: string, retries = 0): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    const r = await fetch(path);
    if (r.ok) return r.json() as Promise<T>;
    if (r.status !== 503 || attempt >= retries) {
      const payload = await r.json().catch(() => null) as { detail?: string } | null;
      throw new Error(payload?.detail ?? `${path} failed: ${r.status}`);
    }
    await new Promise((resolve) => window.setTimeout(resolve, 300 * (attempt + 1)));
  }
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const payload = await r.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `${path} failed: ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export function getTraders(): Promise<TraderInfo[]> {
  return get("/api/traders");
}

export function getRuntime(): Promise<RuntimeInfo> {
  return get("/api/runtime");
}

export function getTrader(name: string): Promise<TraderDetail> {
  return get(`/api/traders/${encodeURIComponent(name)}`, 2);
}

export function getTraderLogs(name: string, lastN = 13): Promise<LogRow[]> {
  return get(`/api/traders/${encodeURIComponent(name)}/logs?last_n=${lastN}`);
}

export function getMarket(): Promise<MarketInfo> {
  return get("/api/market");
}

export function getHealth(): Promise<HealthInfo> {
  return get("/api/health");
}

export function getLatestAgentRun(): Promise<AgentRunRecord | null> {
  return get("/api/agent-runs/latest");
}

export function getAgentRun(runId: string): Promise<AgentRunRecord> {
  return get(`/api/agent-runs/${encodeURIComponent(runId)}`);
}

export function getAgentRunProgress(runId: string): Promise<AgentRunProgress> {
  return get(`/api/agent-runs/${encodeURIComponent(runId)}/progress`);
}

export function createManualAgentRun(idempotencyKey: string): Promise<AgentRunRecord> {
  return post("/api/agent-runs", {
    idempotency_key: idempotencyKey,
    confirm_paper_trading: true,
  });
}

export function cancelAgentRun(runId: string): Promise<AgentRunRecord> {
  return post(`/api/agent-runs/${encodeURIComponent(runId)}/cancel`, {});
}

export function retryAgentRun(runId: string, idempotencyKey: string): Promise<AgentRunRecord> {
  return post(`/api/agent-runs/${encodeURIComponent(runId)}/retry`, {
    idempotency_key: idempotencyKey,
    confirm_paper_trading: true,
  });
}

export function getRiskPolicy(): Promise<RiskPolicyInfo> {
  return get("/api/risk");
}

export function getTraderDecisions(name: string): Promise<DecisionAudit[]> {
  return get(`/api/traders/${encodeURIComponent(name)}/decisions`);
}

export function getEvidence(proposalId: string): Promise<EvidenceChain> {
  return get(`/api/evidence/${encodeURIComponent(proposalId)}`);
}

export interface ReplayScenario {
  scenario_id: string;
  decision_at: string;
  market_timestamp: string;
  retrieved_at: string;
  symbols: string[];
  benchmark_symbol: string;
  source_count: number;
  outcome_available: false;
}

export interface ReplayCatalog {
  dataset: { dataset_id: string; dataset_version: string; scenario_count: number; description: string };
  strategies: string[];
  scenarios: ReplayScenario[];
  notice: string;
}

export interface ReplaySession {
  replay_id: string;
  scenario_id: string;
  strategy: string;
  seed: number;
  status: "decision_complete" | "outcome_revealed";
  decision_at: string;
  market_timestamp: string;
  inputs: {
    prices: Record<string, string>;
    trailing_returns: Record<string, string>;
    sources: Array<{ source_id: string; published_at: string; sentiment: string }>;
  };
  decision: { weights: Record<string, string>; latency_ms: number; model_cost_usd: string };
  outcome: null | {
    outcome_at: string;
    prices: Record<string, string>;
    portfolio_return: string;
    benchmark_return: string;
    benchmark_relative_return: string;
  };
  paper_trading_only: true;
}

export interface AggregateMetrics {
  total_return: string;
  benchmark_return: string;
  benchmark_relative_return: string;
  annualized_volatility: string;
  sharpe: string | null;
  max_drawdown: string;
  turnover: string;
  win_rate: string;
  decision_validity: string;
  citation_validity: string;
  tool_success_rate: string;
  average_latency_ms: string;
  model_api_cost_usd: string;
}

export interface ExperimentReport {
  schema_version: "1.0";
  metadata: {
    run_id: string;
    dataset_id: string;
    dataset_version: string;
    model: string;
    prompt_version: string;
    seed: number;
    completed_at: string;
  };
  results: Array<{ strategy: string; metrics: AggregateMetrics }>;
  leakage_checks_passed: boolean;
}

export function getReplayCatalog(): Promise<ReplayCatalog> { return get("/api/replay/scenarios"); }
export function createReplay(scenario_id: string, strategy: string): Promise<ReplaySession> {
  return post("/api/replays", { scenario_id, strategy, seed: 7 });
}
export function revealReplay(replayId: string): Promise<ReplaySession> {
  return post(`/api/replays/${encodeURIComponent(replayId)}/reveal`);
}
export function getExperiments(): Promise<ExperimentReport[]> { return get("/api/experiments"); }
export function createExperiment(model: string, prompt_version: string): Promise<ExperimentReport> {
  return post("/api/experiments", { model, prompt_version, seed: 7 });
}
