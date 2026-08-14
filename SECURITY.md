# Security model

This application controls paper accounts only. It has no broker, custody, or
real-money execution integration. That boundary reduces financial impact but
does not make model inputs, network tools, credentials, or stored evidence
trusted.

## Trust boundaries and controls

### Prompt injection and malicious research

Web pages, search snippets, citations, and agent memory are untrusted data. The
research prompt explicitly forbids following embedded instructions or allowing
source text to change policy, tools, or the point-in-time cutoff. Research and
trade output must pass strict Pydantic schemas and citation policy. Models can
only propose trades; deterministic risk and execution code independently
validates and persists every approval or rejection.

The live research path calls only Tavily's fixed HTTPS search endpoint through a
project-owned adapter. It requests basic news search with at most five results,
disables generated answers, raw page content, and images, caps each snippet and
the total response bytes, and applies bounded timeouts and retries. Returned
snippets are labeled untrusted in the research prompt and cannot supply their
own source metadata.

### Tool abuse and MCP supply chain

Trading agents receive no account-mutation tools. Account changes are reachable
only through the deterministic approval/execution service. The live research
critical path no longer launches third-party npm MCP packages: its sole search
surface is the narrow Python server owned by this repository. Startup performs
a bounded MCP handshake and attributes failures.

### Secrets and traces

Secrets belong in environment variables or an untracked `.env`, never source,
fixtures, browser bundles, URLs, or prompts. Process environment variables take
precedence over `.env`. The trace configuration disables sensitive input/output
capture, persisted diagnostics are length-bounded and credential-redacted, and
CI scans tracked source for common private-key and provider-token formats.

Container images contain application code and locked dependencies only. Runtime
credentials are injected into the API and scheduler services from the server
environment or an untracked `.env`; they are not Docker build arguments. The
frontend image is static, calls same-origin `/api` routes, and receives no
provider environment variables. The checked-in example trace contains seeded
identifiers only and explicitly excludes model payloads and credentials.

### HTTP API

`API_ACCESS_MODE=local` is the default and assumes loopback access through the
documented Vite proxy. If the API is intentionally exposed, set
`API_ACCESS_MODE=public` and a random `API_AUTH_TOKEN` of at least 32 characters.
Public mode rate-limits requests per directly connected client and requires
Bearer authentication on every mutating method. Put it behind TLS and a trusted
reverse proxy, but do not trust forwarded IP headers without adding an explicit
proxy policy. The in-memory limiter is per process and is not a substitute for
an edge limiter in a multi-instance deployment.

`APP_MODE=demo` is an additional hard boundary: startup requires simulated
market data, all non-safe HTTP methods receive `403`, valuation reads do not
persist observations, and the scheduler refuses to start. This makes the
default container demo inspectable without turning it into a writable public
service. It is not an authorization system for standard mode.

## Container operations

The default Compose ports bind to loopback. For an internet-facing deployment,
terminate TLS at a maintained reverse proxy, set public API authentication,
apply edge rate limits and egress restrictions, store secrets in the platform's
secret manager, run vulnerability scans, and back up the persistent data volume.
Do not expose the scheduler, SQLite volume, or MCP subprocesses as network
services. Rotate a credential if it could have entered logs and remove the logs
according to the incident policy.

## Database protection and recovery

SQLite databases contain account state, research evidence, decisions, health,
and cost metadata and are ignored by git. Schema changes are numbered in
`backend/migrations.py` and apply transactionally at startup. Backups use
SQLite's online backup API, are mode `0600`, and undergo `PRAGMA integrity_check`.

Create and verify a backup:

```bash
uv run python -m backend.database_admin backup backups/accounts-YYYYMMDD.db
uv run python -m backend.database_admin --database backups/accounts-YYYYMMDD.db check
```

Restore only while the API and scheduler are stopped. The command refuses to
replace a target unless `--overwrite` is explicit, validates a temporary copy,
applies migrations, and atomically replaces the destination:

```bash
uv run python -m backend.database_admin restore backups/accounts-YYYYMMDD.db --overwrite
```

For corruption, stop services, preserve the corrupt file for analysis, run
`check`, and restore the newest verified backup. Do not use SQLite repair or
export as an automatic production recovery path.

Decision/evidence/order records are retained indefinitely by default. The
optional prune command removes old logs, valuation-only observations,
unreferenced completed cycle telemetry, and revealed replay sessions; it does
not prune proposal-to-execution audit chains:

```bash
uv run python -m backend.database_admin prune --retention-days 90
```

Backups should be encrypted and retained according to the deployment's data
handling policy. Test restore procedures periodically.

## Dependency and vulnerability checks

CI installs from `uv.lock` and `package-lock.json`, runs Ruff formatting/linting,
Python tests and compilation, frontend tests/type checking/build, the tracked
source secret scan, `pip-audit`, and `npm audit`. Vulnerability findings require
an upgrade or a documented, time-bounded exception tied to a non-reachable code
path.

## Reporting

Do not place credentials or sensitive trace payloads in an issue. Report the
affected component, safe reproduction steps, impact, and the relevant commit.
