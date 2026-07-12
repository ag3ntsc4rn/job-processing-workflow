# job-orchestrator

A small, resilient async job orchestration system. AutoSys (or any producer)
enqueues a job; a dispatcher hands it to Kafka; workers process it. Postgres is
the source of truth, Kafka is transport. See
[`docs/design.md`](docs/design.md) for the full design rationale.

## Layout (monorepo)

| Folder | Component | Role |
|---|---|---|
| `common/` | shared contract | job model, statuses, message envelope, config, `Store` (Postgres + in-memory) |
| `handler/` | producer | dedup-insert a job + outbox row in one transaction (AutoSys stand-in CLI) |
| `handlerAPI/` | HTTP front door | long-running FastAPI service (OAuth2/OIDC resource server) that enqueues + looks up jobs over HTTP; reuses the same enqueue transaction |
| `dispatcher/` | dispatcher | drain the outbox to Kafka (`FOR UPDATE SKIP LOCKED`) |
| `worker/` | worker | consume Kafka, claim (compare-and-set), resolve the effective payload (base config + input overrides), run the job-type handler |
| `reaper/` | reaper | recover jobs stuck in `running` (per-type timeout + churn cap) |
| `migrations/` | schema | idempotent DDL |

**Adding a new job type is a worker-only change**: add a handler in
`worker/handlers/` decorated with `@register("your_type")`, plus one
`job_type_config` row (its `payload`, `run_timeout`, `max_attempts`). The
handler, dispatcher, and reaper are all type-agnostic (`job_type` is just data).

**The message envelope is a pure pointer** — `{job_id, job_type}`. Each
`job_type` has a **base config** in `job_type_config.payload` (the master set of
keys with defaults). Producers (AutoSys/handler) name the job and *may* pass an
optional per-run payload of overrides, stored in `jobs.input_payload`. When the
worker claims the run it resolves the **effective payload** = base config
overlaid with the input (input wins per key), snapshots it into `jobs.payload`
(so each run records the exact config it used), and runs it. The override rides
in the DB row, never on the wire. A type with no config
row is un-runnable: the worker fails such a run gracefully.

## HTTP API (`handlerAPI/`)

`handler/` (the CLI/K8s-Job producer) stays as-is; `handlerAPI/` is a
long-running FastAPI service that fronts the **same** `store.enqueue(...)`
transaction over HTTP so AutoSys, other services, and humans can enqueue and
look up jobs. It never publishes to Kafka or duplicates enqueue SQL. Two
endpoints for now (versioned under `/v1`), plus `/healthz` and `/readyz`:

| Method | Path | Scope | Notes |
|---|---|---|---|
| `POST` | `/v1/jobs` | `jobs.write` | body `{job_type, payload?}`; `201` + `Location`, or `409` if an active job of that type exists |
| `GET` | `/v1/jobs/{id}` | `jobs.read` | ownership-aware (see below); `404` when absent/not visible |

**Auth** — the API is an OAuth2/OIDC **resource server**: every protected call
carries a Ping Federate-issued `Authorization: Bearer <JWT>`. The token is
validated locally against the issuer's JWKS (cached, rotated by `kid`), checking
signature + `iss`/`aud`/`exp`/`nbf` with clock-skew leeway. Both callers work
identically:

- **machine-to-machine** (AutoSys): `client_credentials` (client id + secret) —
  see [`scripts/enqueue_job.sh`](scripts/enqueue_job.sh);
- **human / web app**: authorization-code + PKCE (the SPA obtains the token; the
  full BFF login/logout flow is a later phase).

The principal type (`user` vs `service`) is inferred from the claims, and the
creator (`sub`/`type`/`client_id`) is persisted on the job (`created_by_*`).
Group (AD) claims are captured on the principal so authorization can later key
off Ping's group claims once confirmed. **Scopes are provisional / configurable**
(`jobs.write`, `jobs.read`, `jobs.read.all`).

**Read ownership** — a human user may read only jobs they created; a caller with
`jobs.read.all`, or a service principal, may read any job. Not-visible jobs
return `404` (existence is hidden), not `403`.

**Hardening** — RFC 7807 (`application/problem+json`) errors that never leak SQL
or stack traces, strict request validation, security headers + request-id,
CORS allowlist, and in-app rate limiting. TLS and rate limiting run **in-app for
now** (`TLS_CERTFILE`/`TLS_KEYFILE`, `RATE_LIMIT`) and are expected to move to an
API gateway / load balancer later.

```bash
docker compose up --build -d handlerapi        # also starts mock-oidc (local Ping stand-in)

# get a client-credentials token from the mock issuer (inside the compose network),
# then POST a job and read it back:
tok=$(curl -fsS -X POST http://localhost:8081/default/token \
  -d grant_type=client_credentials -d client_id=autosys-svc -d client_secret=s \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
# NOTE: tokens minted via localhost:8081 have issuer http://localhost:8081/default;
# set OIDC_ISSUER to match, or mint from within the network. See docs/design.md.
```

Configuration is entirely env-driven (`OIDC_ISSUER`, `OIDC_AUDIENCE`,
`OIDC_JWKS_URL`, `SCOPE_*`, `OIDC_GROUPS_CLAIM`, `CORS_ALLOW_ORIGINS`,
`RATE_LIMIT`, `TLS_CERTFILE`/`TLS_KEYFILE`, `HOST`/`PORT`); see
[`handlerAPI/config.py`](handlerAPI/config.py). API deps live in
`requirements-api.txt`.

## Run it (docker compose)

```bash
docker compose up --build -d          # postgres, kafka, migrate, dispatcher, worker, reaper
docker compose run --rm handler hello                       # enqueue with base config only
docker compose run --rm handler hello '{"name":"Grace"}'    # override a base key per run
docker compose logs -f worker         # watch it get processed
```

The `migrate` step seeds `job_type_config` base-config rows for the demo types
`hello` (`{"name": "Ada"}`) and `boom` (`{}`). A producer payload overrides these
per run, e.g. `handler hello '{"name":"Grace"}'` runs with `{"name":"Grace"}`
while `handler hello` runs with the base `{"name":"Ada"}`. To change a type's
base config:

```bash
docker compose exec postgres psql -U app -d app -c \
  "INSERT INTO job_type_config (job_type, payload) VALUES ('hello', '{\"name\":\"Grace\"}')
   ON CONFLICT (job_type) DO UPDATE SET payload = EXCLUDED.payload;"
```

Try the guards:

```bash
# dedup: second enqueue while the first is still active is skipped
docker compose run --rm handler hello
docker compose run --rm handler hello   # -> "skipped: an active job already exists"

# failure path (Option A): lands in `failed`, next schedule re-enqueues
docker compose run --rm handler boom

# scale workers up to the topic partition count
docker compose up -d --scale worker=3
```

Inspect state:

```bash
docker compose exec postgres psql -U app -d app -c \
  "SELECT id, job_type, status, attempts, updated_at FROM jobs ORDER BY id;"
```

## Develop / test

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest                # unit tests run against the in-memory Store (no services)
```
