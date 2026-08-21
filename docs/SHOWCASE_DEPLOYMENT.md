# Recruiter showcase deployment

This deployment runs real, credentialed AI once per configured trading day while
giving public visitors a read-only dashboard. It remains paper trading only.
The API and scheduler run on one host because they share the persistent SQLite
volume declared as `live-data` in `compose.yaml`.

## 1. Prepare the host and DNS

Provision a small Ubuntu host with at least 2 GB RAM and persistent storage.
Create a DNS `A` record for the showcase hostname pointing to the host. Allow
inbound TCP ports 22, 80, and 443 only; the Compose services bind their ports to
loopback.

Install Docker Engine with the Compose plugin, Git, and Caddy using their
official installation instructions. Clone this repository into a directory
such as `/opt/agentic-trading-floor`.

## 2. Configure secrets and runtime behavior

Copy `.env.example` to the untracked `.env` file and set at least:

```dotenv
APP_MODE=standard
PUBLIC_SHOWCASE=true

OPENAI_API_KEY=replace-me
MASSIVE_API_KEY=replace-me
TAVILY_API_KEY=replace-me

MARKET_DATA_MODE=end_of_day
MARKET_DATA_FALLBACK=fail_closed
SCHEDULER_MODE=daily_utc
SCHEDULER_DAILY_TIME_UTC=22:30

API_ACCESS_MODE=public
API_AUTH_TOKEN=replace-with-at-least-32-random-characters
ACCOUNTS_DB=/data/live.db
```

Generate the API token on the server with `openssl rand -hex 32`. Do not put the
token or provider credentials in source control, the browser, a URL, or a Caddy
configuration file.

`PUBLIC_SHOWCASE=true` makes every non-safe HTTP method return `403`. It does not
disable the separate scheduler process, which continues to run the real AI
cycle. `APP_MODE=demo` must not be used for this deployment because demo mode is
seeded, simulated, and prevents scheduler startup.

## 3. Start the application

From the repository root:

```bash
docker compose --profile live up -d --build scheduler live-api live-frontend
docker compose --profile live ps
docker compose --profile live logs --tail=100 scheduler live-api
```

The public frontend is available only on `127.0.0.1:8081` until Caddy is started.
The API is also loopback-only on `127.0.0.1:8000`; neither should be opened in
the host firewall.

## 4. Enable HTTPS

Copy `deploy/Caddyfile.example` to `/etc/caddy/Caddyfile`, replace
`trading.example.com` with the real hostname, validate it, and reload Caddy:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy terminates HTTPS and proxies the dashboard. Its method rule is a second
read-only boundary in front of the application's own showcase middleware.

## 5. Verify the boundary and persistence

Use the real hostname below:

```bash
curl -fsS https://trading.example.com/api/runtime
curl -i -X POST https://trading.example.com/api/agent-runs \
  -H 'Content-Type: application/json' \
  --data '{}'
docker compose --profile live exec live-api \
  python -m backend.database_admin --database /data/live.db check
```

The runtime response should report `public_showcase: true`, `read_only: true`,
and `scheduled_ai_enabled: true`. The POST must return `403` without reaching a
mutation endpoint.

Restart the stack and confirm the existing run history remains visible:

```bash
docker compose --profile live restart
```

The named volume survives restarts and `docker compose down`. Never use
`docker compose down -v` on this deployment because `-v` deletes the database
volume.

## 6. Back up and update

Create a verified backup inside the persistent volume before each update:

```bash
docker compose --profile live exec live-api \
  python -m backend.database_admin --database /data/live.db \
  backup /data/backups/live-YYYYMMDD.db
```

Use a new filename for every backup. Then update without removing the volume:

```bash
git pull --ff-only
docker compose --profile live up -d --build scheduler live-api live-frontend
docker compose --profile live ps
```

Review scheduler and API logs after deployment. Provider credentialed behavior
is deliberately not part of the default test suite, so the first scheduled run
must also be checked in the dashboard and logs.
