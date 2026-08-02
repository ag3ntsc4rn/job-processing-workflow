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
| `handlerAPIv2/` | gateway-fronted front door | same endpoints/schema as `handlerAPI`, but designed to sit **behind an Apigee proxy**: a plain JWT resource server (Keycloak in dev, Apigee-minted JWT in prod), machine-to-machine only, edge concerns delegated to the gateway. See [`handlerAPIv2/README.md`](handlerAPIv2/README.md) |
| `dispatcher/` | dispatcher | drain the outbox to Kafka (`FOR UPDATE SKIP LOCKED`) |
| `worker/` | worker | consume Kafka, claim (compare-and-set), resolve the effective payload (base config + input overrides), run the job-type handler |
| `reaper/` | reaper | recover jobs stuck in `running` (per-type timeout + churn cap) |
| `migrations/` | schema | idempotent DDL |
| `deploy/standalone_pipeline_schema.sql` | schema | one-file DDL for the extracted [job-dispatcher](https://github.com/ag3ntsc4rn/job-dispatcher) + [job-worker](https://github.com/ag3ntsc4rn/job-worker) + [job-reaper](https://github.com/ag3ntsc4rn/job-reaper) services (subset of `migrations/`; not used by this repo) |

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
- **human / web app**: authorization-code + PKCE. In the **target** design the
  SPA never handles tokens — it holds only an httpOnly session cookie, and this
  service (as the BFF) runs PKCE server-side, keeps the access/refresh tokens,
  and attaches the bearer to downstream calls on the browser's behalf. Until
  that BFF login/logout flow lands (a later phase), the SPA can do PKCE itself
  and present the bearer directly; the endpoints don't change, only where the
  token lives.

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

Configuration is entirely env-driven; see the full
[Configuration](#configuration-environment-variables) section below and
[`handlerAPI/config.py`](handlerAPI/config.py). API deps live in
`requirements-api.txt`.

## HTTP API v2 (`handlerAPIv2/`)

`handlerAPIv2/` is a second front door with the **same endpoints and the same
Postgres schema** (no new migrations) but a different **trust model**: it is
built to sit **behind an Apigee API gateway** and be a plain JWT resource server.
Full details in [`handlerAPIv2/README.md`](handlerAPIv2/README.md); the essentials:

- **Auth flow:** customer authenticates to **Keycloak** with client id + secret
  (`client_credentials`) → receives a JWT (scopes + `aud=job-api`) → calls
  **Apigee** with it → Apigee validates at the edge and forwards the request to
  the service **with a JWT** (in the target design Apigee mints a fresh internal
  token). The service just verifies the JWT (signature via JWKS + `iss`/`aud`/`exp`)
  and the scope. *Who signs the token is pure config* — Keycloak in local dev,
  Apigee in prod.
- **Machine-to-machine only:** no human/PKCE, no per-user ownership. Every
  principal is a `service`; any `jobs.read` holder can read any job. `created_by`
  is still recorded.
- **Scopes are admin-granted in Keycloak:** the realm admin attaches `jobs.read`
  / `jobs.write` as client scopes when registering a customer's client; the
  customer can't escalate beyond what was granted. `POST` needs `jobs.write`,
  `GET` needs `jobs.read` (else `403`); an invalid/absent token is `401`.
- **Edge concerns (TLS, CORS, rate limiting) are delegated to the gateway** and
  are absent in-app.
- **Store toggle:** in-memory by default; set `DATABASE_URL` and the
  `PostgresStore` takes over unchanged.

```bash
docker compose up --build -d handlerapiv2   # also starts a real Keycloak with the `jobs` realm

# mint a client-credentials token from Keycloak *inside* the compose network, then POST + GET:
docker compose exec -T handlerapiv2 python - <<'PY'
import httpx
base, kc = "http://localhost:8080", "http://keycloak:8080/realms/jobs/protocol/openid-connect/token"
tok = httpx.post(kc, data={"grant_type": "client_credentials",
                           "client_id": "job-api-v2-client", "client_secret": "job-api-v2-secret"}).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
r = httpx.post(f"{base}/v1/jobs", json={"job_type": "reconcile"}, headers=h)
print(r.status_code, httpx.get(f"{base}{r.headers['Location']}", headers=h).json())
PY
```

## Configuration (environment variables)

Every service reads config from the environment. `common/config.py` is shared by
`handler`, `dispatcher`, `worker`, `reaper`, and the `migrate` step (each uses
only the subset it needs); `handlerAPI/config.py` holds the API-only settings.
All have sensible local defaults so the docker-compose stack works with no extra
setup — the values you **must** set in a real deployment are called out below.

**Hostname note:** sample values use docker-compose service names (`postgres`,
`kafka`, `mock-oidc`) reachable *inside* the compose network. Running a service
outside compose, use `localhost` (the defaults) or your real hostnames.

### Shared — `handler`, `dispatcher`, `worker`, `reaper`, `migrate`

| Variable | Used by | Required? | Default | Sample | Secret? | What it does |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | all | Prod: **yes** | `postgresql://app:app@localhost:5432/app` | `postgresql://app:<password>@postgres:5432/app` | **yes** (holds DB password) | Postgres DSN. Source of truth for jobs/outbox/config. |
| `KAFKA_BOOTSTRAP_SERVERS` | dispatcher, worker | Prod: **yes** | `localhost:9092` | `kafka:9092` | no | Kafka broker list (comma-separated). |
| `KAFKA_TOPIC` | dispatcher, worker | no | `jobs` | `jobs` | no | Topic the pointer envelope is published to / consumed from. Must match across dispatcher and worker. |
| `CONSUMER_GROUP` | worker | no | `workers` | `workers` | no | Kafka consumer group. Keep **stable and shared** across all worker replicas so partitions load-balance instead of each worker reprocessing every message. |
| `DISPATCHER_BATCH_SIZE` | dispatcher | no | `100` | `100` | no | Max outbox rows drained to Kafka per loop. |
| `DISPATCHER_POLL_INTERVAL` | dispatcher | no | `1.0` | `1.0` | no | Seconds to sleep when the outbox is empty. |
| `REAPER_POLL_INTERVAL` | reaper | no | `60.0` | `60.0` | no | Seconds between stuck-`running` sweeps. Run the reaper as a **single replica**. |

The `handler` CLI takes its `job_type` and optional payload as command-line
args (`python -m handler <job_type> [payload_json]`), not env vars. `handler`
and `migrate` only use `DATABASE_URL`; `reaper` uses `DATABASE_URL` +
`REAPER_POLL_INTERVAL` (no Kafka).

### `handlerAPI` (also reads `DATABASE_URL` from the table above)

| Variable | Required? | Default | Sample | Secret? | What it does |
|---|---|---|---|---|---|
| `OIDC_ISSUER` | **yes** | `http://mock-oidc:8080/default` | `https://ping.example.com/as` | no | Expected token `iss`; also used for JWKS discovery when `OIDC_JWKS_URL` is empty. Must exactly match the `iss` your IdP mints. |
| `OIDC_AUDIENCE` | **yes** | `job-api` | `job-api` | no | Required token `aud`. |
| `OIDC_JWKS_URL` | recommended | `` (derived from issuer) | `https://ping.example.com/as/jwks` | no | Signing-key endpoint. If empty, derived from the issuer's discovery document at startup. Set explicitly if the internal JWKS URL differs from the public issuer. |
| `OIDC_CLOCK_SKEW_LEEWAY` | no | `60` | `60` | no | Seconds of tolerance on `exp`/`nbf`/`iat`. |
| `OIDC_JWKS_CACHE_TTL` | no | `3600` | `3600` | no | Seconds to cache JWKS before refetch (keys still rotate by `kid` on miss). |
| `SCOPE_WRITE` | no | `jobs.write` | `jobs.write` | no | Scope required for `POST /v1/jobs`. |
| `SCOPE_READ` | no | `jobs.read` | `jobs.read` | no | Scope required for `GET /v1/jobs/{id}`. |
| `SCOPE_READ_ALL` | no | `jobs.read.all` | `jobs.read.all` | no | Scope that lets a caller read jobs it didn't create. |
| `OIDC_GROUPS_CLAIM` | no | `groups` | `groups` | no | Token claim carrying AD/role groups (captured on the principal for future group-based authz). |
| `CORS_ALLOW_ORIGINS` | no | `` (none) | `https://app.example.com,https://admin.example.com` | no | Comma-separated allowed browser origins. Empty = no cross-origin browser access. |
| `RATE_LIMIT` | no | `60/minute` | `120/minute` | no | In-app rate limit per caller (bearer, IP fallback). |
| `RATE_LIMIT_ENABLED` | no | `true` | `true` | no | Toggle the in-app limiter (disable once a gateway/LB owns it). |
| `HOST` | no | `0.0.0.0` | `0.0.0.0` | no | Bind address. |
| `PORT` | no | `8080` | `8080` | no | Listen port. |
| `TLS_CERTFILE` | only for in-app TLS | unset (plain HTTP) | `/run/secrets/tls/tls.crt` | no (path only) | Server cert path. Set both TLS vars to serve HTTPS in-app; leave unset when a gateway/LB terminates TLS. |
| `TLS_KEYFILE` | only for in-app TLS | unset | `/run/secrets/tls/tls.key` | **yes** (mounted private key) | Server private-key path. Mount as a secret; never inline the key. |

The M2M **client secret** is a *caller* credential (used by AutoSys to obtain a
token) and lives in the caller's secret store — it is **not** an API-server env
var. The API only ever validates the resulting bearer token.

### `handlerAPIv2`

Same OIDC/scope knobs (`OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL`/
`OIDC_CLOCK_SKEW_LEEWAY`/`OIDC_JWKS_CACHE_TTL`/`SCOPE_WRITE`/`SCOPE_READ`/`HOST`/
`PORT`), minus the edge settings the gateway owns (`CORS_ALLOW_ORIGINS`,
`RATE_LIMIT*`, `TLS_*`) and the human/ownership settings it doesn't use
(`SCOPE_READ_ALL`, `OIDC_GROUPS_CLAIM`). Its `DATABASE_URL` is **optional**:
unset → in-memory store, set → Postgres. Full table in
[`handlerAPIv2/README.md`](handlerAPIv2/README.md).

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
