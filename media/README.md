# Portfolio media

Screenshots and a short demo video must be captured from real Compose
deployments; generated mockups are not acceptable evidence. Capture at least:

1. the credentialed overview at `http://localhost:8081` with `STANDARD MODE`,
   `END OF DAY`, and service-health badges visible;
2. the seeded replay and published experiment comparison at
   `http://localhost:8080`, with read-only and limitation copy visible; and
3. a 30–60 second walkthrough moving from overview to replay and experiments.

Before capture, verify `POST /api/replays` returns `403` and that browser network
requests contain no provider key. Remove browser chrome, personal information,
local filesystem paths, and unrelated tabs.

## Captured screenshots

The following screenshots were captured at a 1265 × 712 browser viewport:

- [`screenshots/overview.jpg`](screenshots/overview.jpg) — captured from the
  credentialed live Compose profile on 2026-08-22, showing Massive EOD data,
  healthy services, persisted scheduled-run status, and live paper accounts.
- [`screenshots/replay-lab.jpg`](screenshots/replay-lab.jpg) — point-in-time replay
  configuration captured from the seeded demo on 2026-08-21, with the
  sealed-outcome and read-only boundaries visible.
- [`screenshots/experiments.jpg`](screenshots/experiments.jpg) — published
  single-agent/multi-agent proxy comparison captured from the seeded demo on
  2026-08-21, with the offline limitation copy.

The seeded pre-capture `POST /api/replays` check returned `403` with
`seeded demo mode is read-only`. Both capture-session access logs contained only
same-origin static assets and documented `GET /api/...` paths; neither contained
a provider credential or provider-key query parameter. The live capture did not
trigger a manual run or any HTTP mutation. Browser chrome, unrelated tabs,
personal information, and local filesystem paths are absent from the images.

The 30–60 second walkthrough video remains to be recorded.
