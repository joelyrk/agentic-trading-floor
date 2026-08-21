import {
  cancelAgentRun, createExperiment, createManualAgentRun, createReplay, getAgentRunProgress, getExperiments, getHealth, getLatestAgentRun, getMarket, getReplayCatalog, getRuntime, retryAgentRun,
  getTrader, getTraderDecisions, getTraderLogs, getTraders, revealReplay,
  type AgentRunProgress, type AgentRunRecord, type DecisionAudit, type ExperimentReport, type HealthInfo, type MarketInfo,
  type ReplaySession, type RuntimeInfo, type TraderDetail, type TraderInfo,
} from "./api";
import { initTheme } from "./theme";

initTheme(document.getElementById("btn-theme") as HTMLButtonElement);

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const percent = (value: number) => `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
const escapeHtml = (value: unknown) => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]!);
const runtimePromise = getRuntime();
let runPoll: number | undefined;
let currentRunProgress: AgentRunProgress | null = null;
let currentRoster: TraderInfo[] = [];
let currentTraders: TraderDetail[] = [];
let currentTraderErrors = new Map<string, string>();
let currentLogsByAgent = new Map<string, Array<{ datetime: string; type: string; message: string }>>();
let currentDecisionsByAgent = new Map<string, DecisionAudit[]>();
let refreshedAgentOutputs = new Set<string>();

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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Account data is temporarily unavailable.";
}

function drawdown(points: TraderDetail["time_series"]): number {
  let peak = 0;
  return points.reduce((worst, point) => {
    peak = Math.max(peak, point.value);
    return peak ? Math.max(worst, (peak - point.value) / peak) : worst;
  }, 0);
}

async function loadOverview(): Promise<void> {
  const [runtime, market, health, roster, experiments, latestRun] = await Promise.all([runtimePromise, getMarket(), getHealth(), getTraders(), getExperiments(), getLatestAgentRun()]);
  const runtimeBadge = document.getElementById("runtime-badge")!;
  runtimeBadge.textContent = runtime.public_showcase
    ? "LIVE AI · VIEW ONLY"
    : runtime.read_only ? "SEEDED · READ ONLY" : "STANDARD MODE";
  runtimeBadge.dataset.state = runtime.read_only && !runtime.public_showcase ? "demo" : "good";
  currentRoster = roster;
  const traderResults = await Promise.allSettled(roster.map((item) => getTrader(item.name)));
  const traders = traderResults.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
  currentTraderErrors = new Map(traderResults.flatMap((result, index) => result.status === "rejected"
    ? [[roster[index].name.toLowerCase(), errorMessage(result.reason)] as const]
    : []));
  const logResults = await Promise.allSettled(roster.map(async (item) => ({ name: item.name.toLowerCase(), rows: await getTraderLogs(item.name, 8) })));
  const logs = logResults.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
  currentTraders = traders;
  currentLogsByAgent = new Map(logs.map((item) => [item.name, item.rows]));
  const decisionResults = await Promise.allSettled(roster.map(async (item) => ({ name: item.name, rows: await getTraderDecisions(item.name) })));
  const decisions = decisionResults.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
  currentDecisionsByAgent = new Map(decisions.map((item) => [item.name.toLowerCase(), item.rows]));
  renderStatus(market, health);
  const progress = latestRun ? await getAgentRunProgress(latestRun.run_id) : null;
  renderRunControl(runtime, market, health, latestRun, progress);
  renderAgentDesks(traders, progress, currentLogsByAgent);
  if (latestRun && ["queued", "running"].includes(latestRun.status) && runPoll === undefined) pollRunState(latestRun.run_id);
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
  renderServices(health);
  renderDecisions(allDecisions);
  renderCosts(health);
}

function equityChart(points: TraderDetail["time_series"], chartId: string): string {
  const values = points.length ? points.map((point) => Number(point.value)) : [0];
  const width = 1000;
  const height = 220;
  const padX = 12;
  const padY = 18;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = Math.max(high - low, Math.max(Math.abs(high) * 0.02, 1));
  const floor = low - spread * 0.15;
  const ceiling = high + spread * 0.15;
  const coordinates = values.map((value, index) => {
    const x = padX + (values.length === 1 ? 0 : index / (values.length - 1)) * (width - padX * 2);
    const y = padY + ((ceiling - value) / (ceiling - floor)) * (height - padY * 2);
    return [x, y];
  });
  const line = coordinates.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const last = coordinates.at(-1)!;
  const area = `${line} L${last[0].toFixed(1)},${height} L${coordinates[0][0].toFixed(1)},${height} Z`;
  const firstDate = points[0]?.datetime ? new Date(points[0].datetime).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "Start";
  const lastDate = points.at(-1)?.datetime ? new Date(points.at(-1)!.datetime).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "Now";
  return `<div class="equity-chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Portfolio value from ${escapeHtml(firstDate)} to ${escapeHtml(lastDate)}"><defs><linearGradient id="equity-fill-${chartId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity=".32"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs><path class="chart-area" style="fill:url(#equity-fill-${chartId})" d="${area}"/><path class="chart-line" d="${line}"/></svg><span>${escapeHtml(firstDate)}</span><span>${escapeHtml(lastDate)}</span></div>`;
}

function renderAgentDesks(traders: TraderDetail[], progress: AgentRunProgress | null, logsByAgent: Map<string, Array<{ datetime: string; type: string; message: string }>>): void {
  currentRunProgress = progress;
  const summary = document.getElementById("agent-activity-summary")!;
  summary.textContent = progress ? `${progress.run.trigger} · ${progress.run.status} · ${progress.run.run_id.slice(0, 8)}` : "No run yet";
  const activityByAgent = new Map(progress?.agents.map((agent) => [agent.name.toLowerCase(), agent]) ?? []);
  const host = document.getElementById("agent-desk-list")!;
  host.className = "agent-desk-list";
  const tradersByName = new Map(traders.map((trader) => [trader.name.toLowerCase(), trader]));
  const desks = currentRoster.length ? currentRoster : traders;
  host.innerHTML = desks.map((info) => {
    const deskName = info.name.toLowerCase();
    const trader = tradersByName.get(deskName);
    const activity = activityByAgent.get(deskName);
    if (!trader) {
      const message = currentTraderErrors.get(deskName) ?? "Account data is temporarily unavailable.";
      return `<article class="agent-desk agent-desk-unavailable"><header class="agent-desk-header"><div class="agent-identity"><p>${escapeHtml(info.name.toUpperCase())}</p><span>${escapeHtml(info.model_name)} · ${escapeHtml(info.lastname)}</span></div>${activity ? `<span class="outcome agent-run-state" data-state="${activity.status}">${escapeHtml(activity.status)}</span>` : ""}</header><div class="agent-account-warning"><strong>Account valuation temporarily unavailable</strong><span>${escapeHtml(message)}</span></div></article>`;
    }
    const name = trader.name.toLowerCase();
    const pnlPositive = trader.pnl >= 0;
    const strategy = trader.strategy.replace(/\s+/g, " ").trim();
    const cash = Math.max(Number(trader.balance), 0);
    const total = Math.max(Number(trader.portfolio_value), 1);
    const allocationItems = [...trader.holdings.map((holding) => ({ label: holding.symbol, value: Number(holding.market_value), detail: money.format(holding.market_value), state: holding.unrealized_pnl >= 0 ? "gain" : "loss" })), { label: "CASH", value: cash, detail: money.format(cash), state: "cash" }].filter((item) => item.value > 0);
    const allocation = allocationItems.map((item) => `<div class="allocation-block" data-state="${item.state}" style="flex-grow:${Math.max(item.value / total, 0.015)}"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.detail)}</span></div>`).join("");
    const agentLogs = activity?.logs.length ? activity.logs : (logsByAgent.get(name) ?? []);
    const logRows = agentLogs.length ? agentLogs.map((row) => `<li><time>${escapeHtml(new Date(`${row.datetime}Z`).toLocaleTimeString())}</time><b>${escapeHtml(row.type)}</b><span>${escapeHtml(row.message.split(": ", 2).at(-1) ?? row.message)}</span></li>`).join("") : `<li class="muted">No activity recorded yet.</li>`;
    const trades = trader.transactions.slice(-8).reverse();
    const tradeRows = trades.length ? trades.map((trade) => `<li><time>${escapeHtml(new Date(trade.timestamp).toLocaleDateString(undefined, { month: "2-digit", day: "2-digit" }))}</time><b data-side="${trade.quantity >= 0 ? "buy" : "sell"}">${trade.quantity >= 0 ? "BUY" : "SELL"}</b><span>${Math.abs(trade.quantity)} ${escapeHtml(trade.symbol)} @ ${money.format(trade.price)}</span></li>`).join("") : `<li class="muted">No paper trades yet.</li>`;
    const degraded = trader.valuation_status?.state === "degraded";
    const valuationWarning = degraded
      ? `<div class="agent-account-warning"><strong>Using persisted Massive prices</strong><span>${escapeHtml(trader.valuation_status.error_summary ?? "Live valuation is temporarily unavailable.")}</span></div>`
      : "";
    return `<article class="agent-desk"><header class="agent-desk-header"><div class="agent-identity"><p>${escapeHtml(trader.name.toUpperCase())}</p><span>${escapeHtml(trader.model_name)} · ${escapeHtml(trader.lastname)}</span></div>${activity ? `<span class="outcome agent-run-state" data-state="${activity.status}">${escapeHtml(activity.status)}</span>` : ""}<strong class="agent-value" data-state="${pnlPositive ? "gain" : "loss"}">${money.format(trader.portfolio_value)}</strong><b class="agent-pnl" data-state="${pnlPositive ? "gain" : "loss"}">${pnlPositive ? "+" : "−"}${money.format(Math.abs(trader.pnl))}</b></header>${valuationWarning}<p class="agent-mandate">${escapeHtml(strategy || "No strategy mandate is currently set.")}</p>${equityChart(trader.time_series, name)}<div class="allocation-strip" aria-label="Current portfolio allocation">${allocation}</div><div class="agent-streams"><section><h4>Activity${activity ? ` · ${escapeHtml(activity.current_activity)}` : ""}</h4><ol class="desk-log">${logRows}</ol></section><section><h4>Recent paper trades</h4><ol class="desk-log trade-log">${tradeRows}</ol></section></div></article>`;
  }).join("");
}

function renderRunControl(runtime: RuntimeInfo, market: MarketInfo, health: HealthInfo, run: AgentRunRecord | null, progress: AgentRunProgress | null): void {
  const button = document.getElementById("run-cycle-button") as HTMLButtonElement;
  const status = document.getElementById("run-cycle-status")!;
  const active = run?.status === "queued" || run?.status === "running";
  const sameSnapshot = Boolean(run && market.last_successful_observation && run.market_mode === market.mode && run.market_timestamp === market.last_successful_observation.market_timestamp);
  const cancellable = Boolean(active && run?.trigger === "manual");
  const retryable = Boolean(progress?.can_retry);
  button.textContent = runtime.read_only
    ? runtime.public_showcase ? "Scheduled daily" : "Read-only demo"
    : cancellable
      ? "Cancel run"
      : retryable
        ? "Retry failed run"
        : market.mode === "end_of_day" ? "Run EOD cycle" : "Run paper cycle";
  button.disabled = runtime.read_only || (active && !cancellable) || (!active && !retryable && (sameSnapshot || health.current_cycle_id !== null));
  if (runtime.public_showcase) {
    status.textContent = "Real AI analysis refreshes automatically once per trading day; public controls are disabled.";
  } else if (runtime.read_only) {
    status.textContent = "Manual agent runs are disabled in the seeded demo.";
  } else if (!run) {
    status.textContent = "No coordinated agent run has been recorded yet.";
  } else if (active) {
    status.textContent = cancellable
      ? "Manual run in progress · cancel safely or follow activity below"
      : "Scheduled run in progress · follow agent activity below";
  } else if (retryable) {
    status.textContent = "Failed before producing paper decisions · safe retry available";
  } else if (sameSnapshot) {
    status.textContent = progress?.retry_block_reason
      ? `Retry unavailable · ${progress.retry_block_reason}`
      : "Latest EOD snapshot already used · waiting for new market data";
  } else {
    const completed = run.completed_at ? ` · ${new Date(run.completed_at).toLocaleString()}` : "";
    status.textContent = `${run.trigger} run ${run.status}${completed} · see agent activity below`;
  }
}

async function refreshRunControl(_runId?: string): Promise<AgentRunRecord | null> {
  const [runtime, market, health, run] = await Promise.all([runtimePromise, getMarket(), getHealth(), getLatestAgentRun()]);
  const progress = run ? await getAgentRunProgress(run.run_id) : null;
  renderStatus(market, health);
  renderServices(health);
  renderCosts(health);
  renderRunControl(runtime, market, health, run, progress);
  if (currentTraders.length) renderAgentDesks(currentTraders, progress, currentLogsByAgent);
  else currentRunProgress = progress;
  return run;
}

async function refreshLiveAgentOutputs(progress: AgentRunProgress): Promise<void> {
  const newlyCompleted = progress.agents.filter((agent) =>
    ["succeeded", "failed", "interrupted"].includes(agent.status)
    && !refreshedAgentOutputs.has(agent.name.toLowerCase()));
  if (!newlyCompleted.length) return;
  for (const agent of newlyCompleted) {
    const name = agent.name.toLowerCase();
    const [traderResult, decisionResult] = await Promise.allSettled([
      getTrader(agent.name),
      getTraderDecisions(agent.name),
    ]);
    if (traderResult.status === "fulfilled") {
      currentTraders = [...currentTraders.filter((trader) => trader.name.toLowerCase() !== name), traderResult.value];
      currentTraderErrors.delete(name);
    } else {
      currentTraderErrors.set(name, errorMessage(traderResult.reason));
    }
    if (decisionResult.status === "fulfilled") currentDecisionsByAgent.set(name, decisionResult.value);
    if (traderResult.status === "fulfilled" && decisionResult.status === "fulfilled") refreshedAgentOutputs.add(name);
  }
  renderAgentDesks(currentTraders, progress, currentLogsByAgent);
  const displayNames = new Map(currentTraders.map((trader) => [trader.name.toLowerCase(), trader.name]));
  renderDecisions([...currentDecisionsByAgent.entries()].flatMap(([trader, rows]) =>
    rows.map((row) => ({ trader: displayNames.get(trader) ?? trader, row }))));
}

function pollRunState(runId: string): void {
  if (runPoll !== undefined) window.clearInterval(runPoll);
  refreshedAgentOutputs = new Set();
  runPoll = window.setInterval(async () => {
    try {
      const run = await refreshRunControl(runId);
      if (currentRunProgress) await refreshLiveAgentOutputs(currentRunProgress);
      if (!run || !["queued", "running"].includes(run.status)) {
        window.clearInterval(runPoll);
        runPoll = undefined;
        await loadOverview();
      }
    } catch (error) {
      showError(error);
    }
  }, 3000);
}

document.getElementById("run-cycle-button")!.addEventListener("click", async () => {
  const activeRun = currentRunProgress?.run;
  const action = activeRun && ["queued", "running"].includes(activeRun.status)
    ? "cancel"
    : currentRunProgress?.can_retry ? "retry" : "run";
  const prompt = action === "cancel"
    ? "Cancel this manual agent run? The active agent will be interrupted and the run will remain in the audit history."
    : action === "retry"
      ? "Retry all four agents using the same EOD snapshot? The failed attempt produced no paper decisions; model and research quota will be consumed again."
      : "Run all four paper-trading agents sequentially using the latest market snapshot? This consumes model and research API quota; deterministic risk controls remain enforced.";
  const confirmed = window.confirm(prompt);
  if (!confirmed) return;
  const button = document.getElementById("run-cycle-button") as HTMLButtonElement;
  button.disabled = true;
  button.textContent = "Requesting…";
  try {
    if (action === "cancel" && activeRun) {
      button.textContent = "Cancelling…";
      await cancelAgentRun(activeRun.run_id);
      if (runPoll !== undefined) window.clearInterval(runPoll);
      runPoll = undefined;
      await loadOverview();
      return;
    }
    const run = action === "retry" && activeRun
      ? await retryAgentRun(activeRun.run_id, crypto.randomUUID())
      : await createManualAgentRun(crypto.randomUUID());
    document.getElementById("run-cycle-status")!.textContent = `Manual run ${run.status} · follow agent activity below`;
    await refreshRunControl(run.run_id);
    pollRunState(run.run_id);
  } catch (error) {
    showError(error);
    await refreshRunControl();
  }
});

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
    const approvedQuantity = row.risk_decision?.approved_quantity;
    const quantity = approvedQuantity && approvedQuantity !== row.proposal.quantity
      ? `${row.proposal.quantity} requested → ${approvedQuantity} approved`
      : String(row.proposal.quantity);
    return `<article class="timeline-item"><span class="status-mark" data-state="${outcome}"></span><div><strong>${escapeHtml(trader)} · ${row.proposal.side.toUpperCase()} ${escapeHtml(quantity)} ${escapeHtml(row.proposal.symbol)}</strong><p>${escapeHtml(row.proposal.rationale)}</p><small>${new Date(row.proposal.created_at).toLocaleString()} · ${row.proposal.evidence_claim_ids.length} cited claims</small></div><span class="outcome" data-state="${outcome}">${escapeHtml(outcome.replaceAll("_", " "))}</span></article>`;
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
    button.textContent = runtime.public_showcase ? "View only" : "Read-only demo";
    document.getElementById("dataset-note")!.textContent += runtime.public_showcase
      ? " Public showcase visitors can inspect published results but cannot create records."
      : " Mutations are disabled in the seeded demo.";
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
  const rows = reports.flatMap((report) => report.results.filter((item) => ["single_agent", "multi_agent"].includes(item.strategy)).map((item) => `<tr><th scope="row"><strong>${escapeHtml(report.metadata.model)}</strong><small>${escapeHtml(report.metadata.prompt_version)} · metadata labels · seed ${report.metadata.seed}</small></th><td>${escapeHtml(item.strategy.replaceAll("_", " "))} proxy</td><td>${percent(Number(item.metrics.total_return))}</td><td>${percent(Number(item.metrics.benchmark_relative_return))}</td><td>${percent(-Number(item.metrics.max_drawdown))}</td><td>${percent(Number(item.metrics.turnover))}</td><td>$${Number(item.metrics.model_api_cost_usd).toFixed(4)}</td><td>${Number(item.metrics.average_latency_ms).toFixed(0)}ms</td></tr>`)).join("");
  host.innerHTML = `<table><thead><tr><th>Report labels</th><th>Architecture proxy</th><th>Return</th><th>vs benchmark</th><th>Drawdown</th><th>Turnover</th><th>Estimated cost</th><th>Estimated latency</th></tr></thead><tbody>${rows}</tbody></table><p class="table-note">No model API calls occur. Every row uses immutable point-in-time fixtures and deterministic proxy logic; label changes identify reports but do not change decisions.</p>`;
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
  try { await createExperiment((document.getElementById("model-input") as HTMLInputElement).value, (document.getElementById("prompt-input") as HTMLInputElement).value); await loadExperiments(); } catch (error) { showError(error); } finally { button.disabled = false; button.textContent = "Run deterministic comparison"; }
});

Promise.all([loadOverview(), loadReplay(), loadExperiments()]).catch(showError);
