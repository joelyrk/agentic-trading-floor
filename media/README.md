# Portfolio media

Screenshots and a short demo video must be captured from real Compose
deployments; generated mockups are not acceptable evidence. Capture at least:

1. the credentialed overview at `http://localhost:8081` with `STANDARD MODE`,
   `END OF DAY`, and service-health badges visible;
2. the replay and published experiment comparison, with the point-in-time and
   offline-evaluation limitation copy visible; and
3. a 30–60 second walkthrough moving from overview to replay and experiments.

Before capture, verify `POST /api/replays` returns `403` and that browser network
requests contain no provider key. Remove browser chrome, personal information,
local filesystem paths, and unrelated tabs.

## Captured screenshots

The following screenshots were captured from the portfolio application:

- [`screenshots/overview.jpg`](screenshots/overview.jpg) — captured from the
  credentialed live Compose profile on 2026-08-22, showing Massive EOD data,
  portfolio controls, persisted run status, live paper accounts, and portfolio
  history (exported at 2902 × 1714).
- [`screenshots/overview-audit.jpg`](screenshots/overview-audit.jpg) — the live
  overview's service health, evidence-backed recommendation timeline, and
  observed cost and latency telemetry (exported at 1800 × 1153).
- [`screenshots/replay-lab.jpg`](screenshots/replay-lab.jpg) — point-in-time replay
  configuration captured from the standard Compose profile on 2026-08-22,
  showing a completed multi-agent decision and the outcome revealed only after
  execution (exported at 3786 × 1826).
- [`screenshots/experiments.jpg`](screenshots/experiments.jpg) — published
  single-agent/multi-agent proxy comparison captured from the standard Compose
  profile on 2026-08-22, with the offline limitation copy and metadata-only
  labels visible (exported at 1800 × 839).

The earlier seeded pre-capture `POST /api/replays` check returned `403` with
`seeded demo mode is read-only`. Browser chrome, unrelated tabs, personal
information, and local filesystem paths are absent from the published images.

The 30–60 second walkthrough video remains to be recorded.
