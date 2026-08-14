# Portfolio media

Screenshots and a short demo video must be captured from the real default
Compose deployment at `http://localhost:8080`; generated mockups are not
acceptable evidence. Capture at least:

1. the overview with `SEEDED · READ ONLY` and `SIMULATED` badges visible;
2. the published experiment comparison with the limitation copy visible; and
3. a 30–60 second walkthrough moving from overview to replay and experiments.

Before capture, verify `POST /api/replays` returns `403` and that browser network
requests contain no provider key. Remove browser chrome, personal information,
local filesystem paths, and unrelated tabs. The implementation environment did
not expose the required browser runtime, so no screenshot or video is checked in
as if it had been validated.
