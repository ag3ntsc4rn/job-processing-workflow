# handlerAPIv2 — gateway-fronted HTTP front door

`handlerAPIv2` is a second HTTP front door for the job orchestrator. It exposes
the **same contract** as [`handlerAPI/`](../handlerAPI) — enqueue a job, look one
up, health probes — over the **same** `common.store.Store` enqueue transaction and
the **same Postgres schema** (no new migrations). What changes is the **trust
model**: v2 is designed to sit **behind an Apigee API gateway** and to be a plain
JWT resource server that only ever receives a bearer token carrying claims +
scopes.

| | `handlerAPI` (v1) | `handlerAPIv2` |
|---|---|---|
| Endpoints | `POST /v1/jobs`, `GET /v1/jobs/{id}`, `/healthz`, `/readyz` | **same** |
| Schema | shared `jobs`/`outbox`/`job_type_config` | **same** (no new migrations) |
| Callers | machine (client-credentials) **and** human (auth-code/PKCE) | **machine-to-machine only** |
| Reads | ownership-aware (users read only their own; `jobs.read.all`) | **not ownership-gated** — any `jobs.read` holder reads any job |
| Edge (TLS, CORS, rate limit) | terminated **in-app** | owned by the **gateway** (Apigee); absent here |
| Store | Postgres (real runs) | **in-memory by default**, Postgres when `DATABASE_URL` is set |
| Token issuer | Ping Federate | **Keycloak** (dev) / **Apigee-minted JWT** (prod) — pure config |

## Request flow

```
                 client_credentials
  Customer  ────────(id + secret)───────▶  Keycloak
 (service)  ◀──────── access token (JWT) ──────────┘
     │
     │  Authorization: Bearer <JWT>
     ▼
  Apigee proxy ──── validates token (VerifyJWT / JWKS), quota, spike arrest,
     │              threat protection, TLS ────┐
     │                                          │  mints a new internal JWT
     │  Authorization: Bearer <JWT'>            │  (copies scopes/claims)
     ▼                                          ▼
  handlerAPIv2 ── verifies JWT (signature via JWKS + iss/aud/exp) + scope ──▶ enqueue
```

The customer authenticates to **Keycloak** with a client id + secret
(`client_credentials`) and receives a signed JWT whose `scope` claim carries the
scopes the Keycloak admin granted that client, and whose `aud` is `job-api`. The
customer calls **Apigee** with that token; Apigee validates it at the edge and
forwards the request to `handlerAPIv2` **with a JWT** (in the target design,
Apigee mints a fresh internal JWT after validating the customer's token).

**The service is issuer-agnostic** — who signs the token it receives is pure
config (`OIDC_ISSUER` / `OIDC_JWKS_URL` / `OIDC_AUDIENCE`):

- **local dev / demo:** no gateway in the loop → point at the **Keycloak**
  container directly (the customer's Keycloak token reaches the service);
- **prod:** point at **Apigee's** issuer + JWKS (Apigee mints the JWT).

Same code, same validation — only the env differs.

## Authentication (authn) vs authorization (authz)

Every protected request passes two gates, in order:

1. **authn — is the token genuine?** Select the signing key by `kid` from the
   issuer's JWKS (cached, rotated on `kid` miss), verify the **signature** and
   `iss` / `aud` / `exp` / `nbf` (with clock-skew leeway). Any failure →
   **`401`** with `WWW-Authenticate: Bearer`.
2. **authz — is the caller allowed this operation?** Check the required scope:
   `jobs.write` for `POST /v1/jobs`, `jobs.read` for `GET /v1/jobs/{id}`. Missing
   scope → **`403`**.

So a request is allowed iff the token is authentic **and** carries the required
scope. There is no per-user ownership in v2 (it's machine-to-machine): every
principal is a `service`, and any `jobs.read` holder may read any job. The
enqueuing identity is still recorded on the job (`created_by_*`).

### Who decides a customer's scopes?

The **Keycloak realm administrator**, when registering the customer's client —
never the customer. Scopes are attached to the client as **client scopes**:

- **default client scopes** → always minted into the token (no request needed);
- **optional client scopes** → minted only when the client asks via the `scope=`
  request param, and only if pre-attached.

On `client_credentials`, Keycloak stamps the surviving set into the standard
space-delimited OAuth2 `scope` claim (RFC 6749 §3.3). A client that was granted
only `jobs.read` **cannot** obtain `jobs.write`, no matter what it requests. The
service makes no policy decisions — it only enforces the scopes it receives.

The bundled demo realm ([`deploy/keycloak/jobs-realm.json`](../deploy/keycloak/jobs-realm.json))
ships two clients that demonstrate this:

| Client | Secret (demo only) | Scopes | Can enqueue? |
|---|---|---|---|
| `job-api-v2-client` | `job-api-v2-secret` | `jobs.read`, `jobs.write` | yes |
| `job-api-v2-reader` | `job-api-v2-reader-secret` | `jobs.read` | no → `403` |

## Store selection (demo → prod)

`DATABASE_URL` unset → the app runs on the process-local
`common.store.InMemoryStore` (no infra needed for a demo). Set `DATABASE_URL` and
the real `common.db.PostgresStore` takes over with **no code change** — the same
`Store` protocol, the same enqueue/dedup transaction. The docker-compose
`handlerapiv2` service sets `DATABASE_URL`, so it uses Postgres out of the box.

## Configuration (environment variables)

| Variable | Required? | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | no | *(unset → in-memory)* | Postgres DSN. Unset uses the in-memory store; set switches to Postgres. |
| `OIDC_ISSUER` | **yes** | `http://keycloak:8080/realms/jobs` | Expected token `iss`; JWKS discovery source when `OIDC_JWKS_URL` is empty. Point at Keycloak (dev) or Apigee (prod). |
| `OIDC_AUDIENCE` | **yes** | `job-api` | Required token `aud`. |
| `OIDC_JWKS_URL` | recommended | `` (derived) | Signing-key endpoint. If empty, derived from issuer discovery at startup. |
| `OIDC_CLOCK_SKEW_LEEWAY` | no | `60` | Seconds tolerated on `exp`/`nbf`/`iat`. |
| `OIDC_JWKS_CACHE_TTL` | no | `3600` | Seconds to cache JWKS before refetch (keys still rotate by `kid` on miss). |
| `SCOPE_WRITE` | no | `jobs.write` | Scope required for `POST /v1/jobs`. |
| `SCOPE_READ` | no | `jobs.read` | Scope required for `GET /v1/jobs/{id}`. |
| `HOST` | no | `0.0.0.0` | Bind address. |
| `PORT` | no | `8080` | Listen port. |

Edge concerns — rate limiting, CORS, TLS — are intentionally **not** configurable
here; the API gateway (Apigee) owns them. API deps live in
[`requirements-api.txt`](../requirements-api.txt).

## Run it (docker compose)

```bash
docker compose up --build -d handlerapiv2   # starts postgres, migrate, keycloak, handlerapiv2

# get a client-credentials token from Keycloak (inside the compose network),
# then POST a job and read it back:
docker compose exec -T handlerapiv2 python - <<'PY'
import httpx
base = "http://localhost:8080"
kc = "http://keycloak:8080/realms/jobs/protocol/openid-connect/token"
tok = httpx.post(kc, data={"grant_type": "client_credentials",
                           "client_id": "job-api-v2-client",
                           "client_secret": "job-api-v2-secret"}).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
r = httpx.post(f"{base}/v1/jobs", json={"job_type": "reconcile", "payload": {"region": "eu"}}, headers=h)
print(r.status_code, r.headers.get("Location"))
print(httpx.get(f"{base}{r.headers['Location']}", headers=h).json())
PY
```

Keycloak runs in dev mode, so the token issuer is derived from the request host:
mint tokens from **inside** the compose network (`keycloak:8080`) so `iss` matches
the service's `OIDC_ISSUER`. The Keycloak admin console is exposed on the host at
`http://localhost:8083` (admin / admin) for inspecting the realm.

## Develop / test

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest                # unit + HTTP tests run against the in-memory store; tokens are minted locally, no live IdP
```

Tests live in [`tests/test_handler_api_v2.py`](../tests/test_handler_api_v2.py)
(authn 401s, authz 403s, create/dedup, validation, non-owned reads, hardening,
store selection) and [`tests/test_auth_v2.py`](../tests/test_auth_v2.py) (verifier,
scope extraction, JWKS cache). CI additionally runs a Keycloak-backed end-to-end
check (`401 → 403 → 201 → 200 → 409`) and `pip-audit` over the API dependencies.
