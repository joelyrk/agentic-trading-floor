// App entry point: build a panel per trader, then poll the backend for portfolio
// data and activity logs. The trading floor runs on its own; this only reads.

import { getHealth, getMarket, getTrader, getTraderDecisions, getTraderLogs, getTraders } from "./api";
import { TraderPanel } from "./panel";
import { TraderState } from "./state";
import { initTheme } from "./theme";

const DATA_POLL_MS = 6000;
const LOG_POLL_MS = 2000;

initTheme(document.getElementById("btn-theme") as HTMLButtonElement);

const panelHost = document.getElementById("panels")!;
const states = new Map<string, TraderState>();
const panels = new Map<string, TraderPanel>();

async function loadMarket(): Promise<void> {
  try {
    const market = await getMarket();
    const badge = document.getElementById("market-badge")!;
    badge.dataset.mode = market.mode;
    badge.dataset.degraded = String(market.degraded);
    const labels: Record<typeof market.mode, string> = {
      end_of_day: "END OF DAY",
      delayed: "DELAYED",
      real_time: "REAL TIME",
      simulated: "SIMULATED",
    };
    document.getElementById("market-source")!.textContent = labels[market.mode];
    const freshness = `freshness limit ${market.freshness_threshold_seconds}s`;
    const availability = market.mode === "end_of_day"
      ? "Previous close"
      : market.mode === "simulated"
        ? "Simulator available"
        : market.is_market_open ? "Market open" : "Market closed";
    const state = market.degraded
      ? `Degraded · ${market.error_summary ?? "provider fallback active"}`
      : availability;
    document.getElementById("market-status")!.textContent = `${state} · ${freshness}`;
  } catch (err) {
    console.error("market fetch failed", err);
  }
}

async function loadHealth(): Promise<void> {
  try {
    const health = await getHealth();
    const summary = document.getElementById("health-summary")!;
    summary.textContent = health.current_cycle_id
      ? `${health.status.toUpperCase()} · cycle ${health.current_cycle_id.slice(0, 8)}`
      : health.status.toUpperCase();
    summary.dataset.state = health.status;
    const services = document.getElementById("health-services")!;
    services.innerHTML = "";
    for (const service of health.services) {
      const row = document.createElement("div");
      row.className = "health-service";
      row.dataset.state = service.state;
      row.title = service.error_summary ?? (service.last_success ? `Last success ${service.last_success}` : "Not checked yet");
      row.innerHTML = `<span class="health-dot"></span><span></span><span></span>`;
      row.children[1].textContent = service.name;
      row.children[2].textContent = service.latency_ms === null ? service.state : `${service.latency_ms.toFixed(0)}ms`;
      services.append(row);
    }
    document.getElementById("health-metrics")!.textContent =
      `${health.metrics.cycle_success_rate.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 0 })} cycles · ` +
      `${health.metrics.token_count.toLocaleString()} tokens · $${health.metrics.estimated_cost_usd.toFixed(4)}`;
  } catch (err) {
    const summary = document.getElementById("health-summary")!;
    summary.textContent = "UNAVAILABLE";
    summary.dataset.state = "degraded";
    console.error("health fetch failed", err);
  }
}

async function buildPanels(): Promise<void> {
  const traders = await getTraders();
  for (const info of traders) {
    const state = new TraderState(info);
    const panel = new TraderPanel(state);
    states.set(info.name, state);
    panels.set(info.name, panel);
    panelHost.append(panel.root);
    panel.mount();
  }
}

async function pollData(): Promise<void> {
  await Promise.all(
    [...states].map(async ([name, state]) => {
      try {
        state.recordDetail(await getTrader(name));
        panels.get(name)!.update();
        panels.get(name)!.renderDecisions(await getTraderDecisions(name));
      } catch (err) {
        console.error(`data fetch failed for ${name}`, err);
      }
    }),
  );
  markLeader();
  renderReturns();
}

function renderReturns(): void {
  const list = document.getElementById("returns-list")!;
  const rows = [...states.values()]
    .map((s) => s.detail)
    .filter((d): d is NonNullable<typeof d> => d !== null)
    .map((d) => {
      const initial = d.portfolio_value - d.pnl; // each trader started with this
      return { name: d.name, pct: initial > 0 ? (d.pnl / initial) * 100 : 0 };
    })
    .sort((a, b) => b.pct - a.pct);
  list.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.className = "returns-row";
    const name = document.createElement("span");
    name.className = "returns-name";
    name.textContent = r.name;
    const pct = document.createElement("span");
    pct.className = "returns-pct";
    pct.dataset.trend = r.pct >= 0 ? "up" : "down";
    pct.textContent = `${r.pct >= 0 ? "+" : ""}${r.pct.toFixed(1)}%`;
    li.append(name, pct);
    list.append(li);
  }
}

async function pollLogs(): Promise<void> {
  await Promise.all(
    [...panels].map(async ([name, panel]) => {
      try {
        panel.renderLogs(await getTraderLogs(name));
      } catch (err) {
        console.error(`log fetch failed for ${name}`, err);
      }
    }),
  );
}

function markLeader(): void {
  const values = [...states.values()].map((s) => s.detail?.portfolio_value).filter((v): v is number => v !== undefined);
  const best = Math.max(...values);
  // Only crown a single clear leader; a tie (e.g. before any trading) highlights nobody.
  const unique = values.filter((v) => v === best).length === 1;
  for (const [name, state] of states) {
    panels.get(name)!.setLeader(unique && state.detail?.portfolio_value === best);
  }
}

async function main(): Promise<void> {
  await loadMarket();
  await loadHealth();
  await buildPanels();
  await pollData();
  await pollLogs();
  setInterval(loadMarket, DATA_POLL_MS);
  setInterval(loadHealth, DATA_POLL_MS);
  setInterval(pollData, DATA_POLL_MS);
  setInterval(pollLogs, LOG_POLL_MS);
}

main().catch((err) => console.error("startup failed", err));
