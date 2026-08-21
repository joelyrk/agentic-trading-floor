import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const client = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const nginx = await readFile(new URL("../nginx.conf", import.meta.url), "utf8");

test("decision console exposes keyboard and assistive-technology landmarks", () => {
  assert.match(html, /class="skip-link"/);
  assert.match(html, /<nav[^>]+aria-label="Primary navigation"/);
  assert.match(html, /<main id="content">/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /role="alert"/);
});

test("replay keeps outcome reveal as a separate labeled action", () => {
  assert.match(html, /Outcomes stay sealed until the decision is complete/);
  assert.match(html, /Run paper decision/);
  assert.doesNotMatch(html, /outcome_available\s*:\s*true/);
});

test("responsive and reduced-motion styles are present", () => {
  assert.match(css, /@media\(max-width:650px\)/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)/);
  assert.match(css, /:focus-visible/);
});

test("manual agent runs require confirmation and expose live status", () => {
  assert.match(html, /id="run-cycle-button"[^>]+disabled/);
  assert.match(html, /id="run-cycle-status"[^>]+aria-live="polite"/);
  assert.match(client, /window\.confirm/);
  assert.match(client, /createManualAgentRun\(crypto\.randomUUID\(\)\)/);
});

test("public showcase clearly separates view-only access from scheduled live AI", () => {
  assert.match(client, /LIVE AI · VIEW ONLY/);
  assert.match(client, /Real AI analysis refreshes automatically once per trading day/);
  assert.match(client, /runtime\.public_showcase/);
});

test("overview exposes each agent's strategy in the agent desk grid", () => {
  assert.match(html, /<h3>Agent desks<\/h3>/);
  assert.match(html, /id="agent-desk-list"/);
  assert.match(client, /renderAgentDesks\(traders,/);
  assert.match(client, /trader\.strategy/);
  assert.match(css, /\.agent-desk-list\{display:grid;grid-template-columns:repeat\(2,/);
});

test("frontend proxy re-resolves a recreated API container", () => {
  assert.match(nginx, /resolver 127\.0\.0\.11 valid=10s ipv6=off;/);
  assert.match(nginx, /set \$api_upstream \$\{API_UPSTREAM\};/);
  assert.match(nginx, /proxy_pass http:\/\/\$api_upstream;/);
});

test("experiments disclose metadata-only labels and deterministic proxies", () => {
  assert.match(html, /Model and prompt values below are report labels only/);
  assert.match(html, /changing them does not call that model/);
  assert.match(client, /Architecture proxy/);
  assert.match(client, /No model API calls occur/);
});

test("active run polling refreshes decisions and agent account outputs", () => {
  assert.match(client, /async function refreshLiveAgentOutputs/);
  assert.match(client, /newlyCompleted = progress\.agents\.filter/);
  assert.match(client, /getTrader\(agent\.name\)/);
  assert.match(client, /getTraderDecisions\(agent\.name\)/);
  assert.match(client, /refreshedAgentOutputs\.add\(name\)/);
  assert.match(client, /if \(currentRunProgress\) await refreshLiveAgentOutputs\(currentRunProgress\)/);
});

test("resized paper decisions show requested and approved quantities", () => {
  assert.match(client, /requested →/);
  assert.match(client, /approved_quantity/);
});

test("agent refresh failures stay local and live telemetry is repainted", () => {
  assert.match(client, /Promise\.allSettled\(roster\.map\(\(item\) => getTrader/);
  assert.match(client, /Account valuation temporarily unavailable/);
  assert.match(client, /renderCosts\(health\)/);
  assert.match(client, /renderServices\(health\)/);
  assert.match(css, /\.agent-account-warning/);
});

test("account warnings reserve a grid row without shifting charts or allocation", () => {
  assert.match(css, /grid-template-rows:150px auto 108px 230px 90px auto/);
  assert.match(css, />\.agent-account-warning\{grid-row:2\}/);
  assert.match(css, />\.equity-chart\{grid-row:4\}/);
  assert.match(css, />\.allocation-strip\{grid-row:5\}/);
  assert.match(css, />\.agent-streams\{grid-row:6\}/);
  assert.match(css, /\.agent-streams h4\{min-height:32px/);
  assert.doesNotMatch(css, /\.agent-streams h4\{height:32px/);
  assert.match(css, /\.agent-streams>section\{display:grid;grid-row:span 2;grid-template-rows:subgrid/);
  assert.match(css, /@media\(max-width:1200px\).*\.agent-streams>section\{display:block;grid-row:auto\}/);
  assert.match(css, /@media\(max-width:900px\).*grid-template-rows:150px auto auto 210px 90px auto/);
});

test("activity timestamps preserve existing timezone offsets", () => {
  assert.match(client, /timestampDate/);
  assert.match(client, /\(\?:Z\|\[\+-\]/);
  assert.doesNotMatch(client, /new Date\(`\$\{row\.datetime\}Z`\)/);
});
