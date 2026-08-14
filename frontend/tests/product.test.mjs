import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const client = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");

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

test("overview exposes each agent's strategy in the agent desk grid", () => {
  assert.match(html, /<h3>Agent desks<\/h3>/);
  assert.match(html, /id="agent-desk-list"/);
  assert.match(client, /renderAgentDesks\(traders,/);
  assert.match(client, /trader\.strategy/);
  assert.match(css, /\.agent-desk-list\{display:grid;grid-template-columns:repeat\(2,/);
});
