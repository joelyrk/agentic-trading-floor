import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

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
