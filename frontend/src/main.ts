import {
  createExperiment, createReplay, getExperiments, getHealth, getMarket, getReplayCatalog, getRiskPolicy, getRuntime,
  getTrader, getTraderDecisions, getTraders, revealReplay,
  type DecisionAudit, type ExperimentReport, type HealthInfo, type MarketInfo,
  type ReplaySession, type RiskPolicyInfo, type TraderDetail,
} from "./api";
import { initTheme } from "./theme";

initTheme(document.getElementById("btn-theme") as HTMLButtonElement);

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const percent = (value: number) => `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
const escapeHtml = (value: unknown) => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]!);
const runtimePromise = getRuntime();

document.querySelectorAll<HTMLButtonElement>(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll<HTMLButtonElement>(".tab").forEach((item) => {
    const active = item === tab;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-selected", String(active));
    document.getElementById(`${item.dataset.view}-view`)!.hidden = !active;
  });
}));

function showError(error: unknown): void {
  const host = document.getElementById("global-error")!;
  host.textContent = error instanceof Error ? error.message : "Something went wrong.";
  host.hidden = false;
}

function drawdown(points: TraderDetail["time_series"]): number {
  let peak = 0;
  return points.reduce((worst, point) => {
    peak = Math.max(peak, point.value);
    return peak ? Math.max(worst, (peak - point.value) / peak) : worst;
  }, 0);
}

function turnover(trader: TraderDetail): number {
  const base = trader.portfolio_value - trader.pnl;
  return base > 0 ? trader.transactions.reduce((sum, tx) => sum + Math.abs(tx.quantity * tx.price), 0) / base : 0;
}

async function loadOverview(): Promise<void> {
  const [runtime, market, health, roster, risk, experiments] = await Promise.all([runtimePromise, getMarket(), getHealth(), getTraders(), getRiskPolicy(), getExperiments()]);
  const runtimeBadge = document.getElementById("runtime-badge")!;
  runtimeBadge.textContent = runtime.read_only ? "SEEDED · READ ONLY" : "STANDARD MODE";
  runtimeBadge.dataset.state = runtime.read_only ? "demo" : "good";
  const traders = await Promise.all(roster.map((item) => getTrader(item.name)));
  const decisions = (await Promise.all(roster.map(async (item) => ({ name: item.name, rows: await getTraderDecisions(item.name) }))));
  renderStatus(market, health);
  const total = traders.reduce((sum, item) => sum + item.portfolio_value, 0);
  const initial = traders.reduce((sum, item) => sum + item.portfolio_value - item.pnl, 0);
  const allDecisions = decisions.flatMap((item) => item.rows.map((row) => ({ trader: item.name, row })));
  const approved = allDecisions.filter(({ row }) => row.risk_decision?.outcome === "approved").length;
  const rejected = allDecisions.filter(({ row }) => row.risk_decision?.outcome === "rejected").length;
  const latestAgent = experiments[0]?.results.find((item) => item.strategy === "multi_agent");
  document.getElementById("overview-metrics")!.innerHTML = [
    ["Portfolio value", money.format(total), percent(initial ? (total - initial) / initial : 0)],
    ["Maximum drawdown", percent(-Math.max(...traders.map((item) => drawdown(item.time_series)), 0)), "Peak-to-trough"],
    ["Proposal controls", `${approved} approved`, `${rejected} rejected`],
    ["Latest replay vs benchmark", latestAgent ? percent(Number(latestAgent.metrics.benchmark_relative_return)) : "Not run", latestAgent ? `${experiments[0].metadata.model} · offline` : "Run an experiment to compare"],
  ].map(([label, value, note]) => `<article class="metric"><span>${label}</span><strong>${value}</strong><small>${note}</small></article>`).join("");
  renderPortfolios(traders, risk);
  renderServices(health);
  renderDecisions(allDecisions);
  renderCosts(health);
}

function renderStatus(market: MarketInfo, health: HealthInfo): void {
  const marketBadge = document.getElementById("market-badge")!;
  marketBadge.textContent = `${market.mode.replaceAll("_", " ").toUpperCase()}${market.degraded ? " · DEGRADED" : ""}`;
  marketBadge.dataset.state = market.degraded ? "bad" : "good";
  const healthBadge = document.getElementById("health-badge")!;
  healthBadge.textContent = health.status.toUpperCase();
  healthBadge.dataset.state = health.status === "healthy" ? "good" : "bad";
  const observation = market.last_successful_observation;
  document.getElementById("freshness-copy")!.textContent = observation
    ? `${market.provider} · ${market.mode.replaceAll("_", " ")} · market timestamp ${new Date(observation.market_timestamp).toLocaleString()} · ${observation.is_stale ? "stale" : "fresh"}`
    : `${market.provider} · ${market.mode.replaceAll("_", " ")} · no observation recorded yet`;
}

function renderPortfolios(traders: TraderDetail[], risk: RiskPolicyInfo): void {
  const symbolLimit = Number(risk.max_symbol_concentration);
  const rows = traders.map((trader) => {
    const concentration = trader.portfolio_value ? Math.max(0, ...trader.holdings.map((h) => h.market_value / trader.portfolio_value)) : 0;
    const utilization = symbolLimit > 0 ? Math.min(concentration / symbolLimit, 1) : 0;
    return `<tr><th scope="row"><strong>${escapeHtml(trader.name)}</strong><small>${escapeHtml(trader.model_name)}</small></th><td>${money.format(trader.portfolio_value)}<small>${percent((trader.portfolio_value - trader.pnl) ? trader.pnl / (trader.portfolio_value - trader.pnl) : 0)} return</small></td><td>${percent(-drawdown(trader.time_series))}</td><td>${percent(turnover(trader))}</td><td><div class="meter" aria-label="${(utilization * 100).toFixed(0)} percent of symbol concentration limit"><i style="width:${utilization * 100}%"></i></div><small>${percent(concentration)} / ${(symbolLimit * 100).toFixed(0)}% limit</small></td></tr>`;
  }).join("");
  document.getElementById("portfolio-table")!.className = "table-wrap";
  document.getElementById("portfolio-table")!.innerHTML = `<table><thead><tr><th>Account</th><th>Portfolio vs start</th><th>Drawdown</th><th>Turnover</th><th>Risk utilization</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderServices(health: HealthInfo): void {
  document.getElementById("service-summary")!.textContent = `${health.services.filter((item) => item.state === "healthy").length}/${health.services.length} healthy`;
  const host = document.getElementById("service-list")!;
  host.className = "stack";
  host.innerHTML = health.services.length ? health.services.map((service) => `<div class="service"><span class="dot" data-state="${service.state}"></span><div><strong>${escapeHtml(service.name)}</strong><small>${escapeHtml(service.error_summary ?? service.state)}</small></div><span>${service.latency_ms === null ? "—" : `${service.latency_ms.toFixed(0)}ms`}</span></div>`).join("") : `<div class="empty-state">No service checks have been recorded.</div>`;
}

function renderDecisions(items: Array<{ trader: string; row: DecisionAudit }>): void {
  const host = document.getElementById("decision-list")!;
  host.className = "timeline";
  const sorted = items.sort((a, b) => b.row.proposal.created_at.localeCompare(a.row.proposal.created_at));
  host.innerHTML = sorted.length ? sorted.slice(0, 10).map(({ trader, row }) => {
    const outcome = row.risk_decision?.outcome ?? "pending";
    return `<article class="timeline-item"><span class="status-mark" data-state="${outcome}"></span><div><strong>${escapeHtml(trader)} · ${row.proposal.side.toUpperCase()} ${row.proposal.quantity} ${escapeHtml(row.proposal.symbol)}</strong><p>${escapeHtml(row.proposal.rationale)}</p><small>${new Date(row.proposal.created_at).toLocaleString()} · ${row.proposal.evidence_claim_ids.length} cited claims</small></div><span class="outcome" data-state="${outcome}">${escapeHtml(outcome.replaceAll("_", " "))}</span></article>`;
  }).join("") : `<div class="empty-state">No proposals yet. Once an agent proposes a paper trade, its evidence and control outcome will appear here.</div>`;
}

function renderCosts(health: HealthInfo): void {
  const metrics = health.metrics;
  const host = document.getElementById("cost-panel")!;
  host.className = "stack";
  host.innerHTML = `<div class="key-value"><span>Estimated spend</span><strong>$${metrics.estimated_cost_usd.toFixed(4)}</strong></div><div class="key-value"><span>Tokens</span><strong>${metrics.token_count.toLocaleString()}</strong></div><div class="key-value"><span>Average latency</span><strong>${metrics.average_cycle_latency_ms.toFixed(0)}ms</strong></div><div class="key-value"><span>Cycle success</span><strong>${percent(metrics.cycle_success_rate)}</strong></div><div class="key-value"><span>MCP failure rate</span><strong>${percent(metrics.mcp_failure_rate)}</strong></div>`;
}

async function loadReplay(): Promise<void> {
  const [runtime, catalog] = await Promise.all([runtimePromise, getReplayCatalog()]);
  const scenario = document.getElementById("scenario-select") as HTMLSelectElement;
  const strategy = document.getElementById("strategy-select") as HTMLSelectElement;
  scenario.innerHTML = catalog.scenarios.map((item) => `<option value="${escapeHtml(item.scenario_id)}">${escapeHtml(item.scenario_id)} · ${new Date(item.decision_at).toLocaleDateString()}</option>`).join("");
  strategy.innerHTML = catalog.strategies.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item.replaceAll("_", " "))}</option>`).join("");
  strategy.value = "multi_agent";
  document.getElementById("dataset-note")!.textContent = `${catalog.dataset.dataset_id} · version ${catalog.dataset.dataset_version} · ${catalog.dataset.scenario_count} scenarios. ${catalog.notice}`;
  if (runtime.read_only) {
    const button = document.querySelector<HTMLButtonElement>("#replay-form button")!;
    button.disabled = true;
    button.textContent = "Read-only demo";
    document.getElementById("dataset-note")!.textContent += " Mutations are disabled in the seeded demo.";
  }
}

function renderReplay(session: ReplaySession): void {
  document.getElementById("replay-state")!.textContent = session.status.replaceAll("_", " ");
  const allocations = Object.entries(session.decision.weights);
  const result = document.getElementById("replay-result")!;
  result.className = "replay-detail";
  result.innerHTML = `<div class="seal"><span>Decision complete</span><strong>${escapeHtml(session.scenario_id)}</strong></div><div class="key-value"><span>Architecture</span><strong>${escapeHtml(session.strategy.replaceAll("_", " "))}</strong></div><div class="key-value"><span>Market cutoff</span><strong>${new Date(session.market_timestamp).toLocaleString()}</strong></div><div><h4>Paper allocation</h4>${allocations.length ? allocations.map(([symbol, weight]) => `<div class="allocation"><span>${escapeHtml(symbol)}</span><i style="width:${Number(weight) * 100}%"></i><strong>${percent(Number(weight))}</strong></div>`).join("") : `<p class="muted">No trade proposed.</p>`}</div>${session.outcome ? `<div class="outcome-panel"><h4>Outcome revealed</h4><div class="key-value"><span>Portfolio return</span><strong>${percent(Number(session.outcome.portfolio_return))}</strong></div><div class="key-value"><span>Benchmark return</span><strong>${percent(Number(session.outcome.benchmark_return))}</strong></div><div class="key-value"><span>Relative return</span><strong>${percent(Number(session.outcome.benchmark_relative_return))}</strong></div></div>` : `<button id="reveal-button" class="secondary" type="button">Reveal withheld outcome</button><p class="fine-print">This separate action proves the decision record existed before outcome access.</p>`}`;
  document.getElementById("reveal-button")?.addEventListener("click", async () => {
    const button = document.getElementById("reveal-button") as HTMLButtonElement;
    button.disabled = true; button.textContent = "Revealing…";
    try { renderReplay(await revealReplay(session.replay_id)); } catch (error) { showError(error); }
  });
}

document.getElementById("replay-form")!.addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = document.getElementById("replay-result")!;
  result.className = "loading"; result.textContent = "Running point-in-time decision…";
  try { renderReplay(await createReplay((document.getElementById("scenario-select") as HTMLSelectElement).value, (document.getElementById("strategy-select") as HTMLSelectElement).value)); } catch (error) { result.className = "empty-state"; result.textContent = "Replay failed."; showError(error); }
});

function renderExperiments(reports: ExperimentReport[]): void {
  const host = document.getElementById("experiments-list")!;
  host.className = "card table-wrap";
  if (!reports.length) { host.innerHTML = `<div class="empty-state">No experiments recorded. Run one to compare baselines, single-agent, and multi-agent architectures.</div>`; return; }
  const rows = reports.flatMap((report) => report.results.filter((item) => ["single_agent", "multi_agent"].includes(item.strategy)).map((item) => `<tr><th scope="row"><strong>${escapeHtml(report.metadata.model)}</strong><small>${escapeHtml(report.metadata.prompt_version)} · seed ${report.metadata.seed}</small></th><td>${escapeHtml(item.strategy.replaceAll("_", " "))}</td><td>${percent(Number(item.metrics.total_return))}</td><td>${percent(Number(item.metrics.benchmark_relative_return))}</td><td>${percent(-Number(item.metrics.max_drawdown))}</td><td>${percent(Number(item.metrics.turnover))}</td><td>$${Number(item.metrics.model_api_cost_usd).toFixed(4)}</td><td>${Number(item.metrics.average_latency_ms).toFixed(0)}ms</td></tr>`)).join("");
  host.innerHTML = `<table><thead><tr><th>Model / prompt</th><th>Architecture</th><th>Return</th><th>vs benchmark</th><th>Drawdown</th><th>Turnover</th><th>Cost</th><th>Latency</th></tr></thead><tbody>${rows}</tbody></table><p class="table-note">All rows use immutable point-in-time fixtures. Identical returns across model labels are expected for the current deterministic architecture proxies.</p>`;
}

async function loadExperiments(): Promise<void> {
  const [runtime, reports] = await Promise.all([runtimePromise, getExperiments()]);
  renderExperiments(reports);
  if (runtime.read_only) {
    const button = document.querySelector<HTMLButtonElement>("#experiment-form button")!;
    button.disabled = true;
    button.textContent = "Published results";
    for (const field of document.querySelectorAll<HTMLInputElement>("#experiment-form input")) field.disabled = true;
  }
}
document.getElementById("experiment-form")!.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = (event.currentTarget as HTMLFormElement).querySelector("button")!;
  button.disabled = true; button.textContent = "Running 30 scenarios…";
  try { await createExperiment((document.getElementById("model-input") as HTMLInputElement).value, (document.getElementById("prompt-input") as HTMLInputElement).value); await loadExperiments(); } catch (error) { showError(error); } finally { button.disabled = false; button.textContent = "Run experiment"; }
});

Promise.all([loadOverview(), loadReplay(), loadExperiments()]).catch(showError);
