// Client for the trading floor HTTP API. All paths are relative; in dev the Vite
// proxy forwards /api to the FastAPI backend, so the browser sees one origin.

export interface TraderInfo {
  name: string;
  lastname: string;
  model_name: string;
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

export interface DecisionAudit {
  proposal: TradeProposal;
  risk_decision: { outcome: "approved" | "rejected" | "pending_human" } | null;
  order: unknown | null;
  execution: { status: string } | null;
}

export interface EvidenceChain {
  research: ResearchBrief;
  prompt_versions: { researcher: string; trader: string };
  proposal: TradeProposal;
  market_observation: MarketObservation;
  risk_decision: { outcome: string; rules: Array<{ rule: string; passed: boolean; reason: string }> } | null;
  order: unknown | null;
  execution: { status: string; executed_at: string } | null;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} failed: ${r.status}`);
  return r.json() as Promise<T>;
}

export function getTraders(): Promise<TraderInfo[]> {
  return get("/api/traders");
}

export function getTrader(name: string): Promise<TraderDetail> {
  return get(`/api/traders/${encodeURIComponent(name)}`);
}

export function getTraderLogs(name: string, lastN = 13): Promise<LogRow[]> {
  return get(`/api/traders/${encodeURIComponent(name)}/logs?last_n=${lastN}`);
}

export function getMarket(): Promise<MarketInfo> {
  return get("/api/market");
}

export function getTraderDecisions(name: string): Promise<DecisionAudit[]> {
  return get(`/api/traders/${encodeURIComponent(name)}/decisions`);
}

export function getEvidence(proposalId: string): Promise<EvidenceChain> {
  return get(`/api/evidence/${encodeURIComponent(proposalId)}`);
}
