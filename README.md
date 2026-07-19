# job-api

A small FastAPI service for enqueuing and reading jobs, built to run **behind an
API gateway (Apigee)**. It is a plain JWT *resource server*: it validates the
Bearer token it receives and enforces per-endpoint OAuth2 scopes. It is
machine-to-machine only.

This is a standalone extraction of the `handlerAPIv2` component — it contains
only the code, tests, and config for this one service (no Keycloak, no
docker-compose, no sibling components).

## Endpoints

| Method | Path             | Auth            | Description                          |
|--------|------------------|-----------------|--------------------------------------|
| `POST` | `/v1/jobs`       | `jobs.write`    | Enqueue a job. `201` + `Location`.   |
| `GET`  | `/v1/jobs/{id}`  | `jobs.read`     | Fetch a job. `200` / `404`.          |
| `GET`  | `/healthz`       | none            | Liveness. `{"status":"ok"}`.         |
| `GET`  | `/readyz`        | none            | Readiness (checks the store). `503` if down. |

Errors are RFC 7807 `application/problem+json`. Internal failures are masked as
`500` with no internals leaked.

## Trust model

```
customer --(client_id/secret)--> IdP  ->  access token
customer --(Bearer token)------> Apigee (validates, mints internal JWT)
Apigee   --(Bearer JWT)--------> job-api (validates signature/iss/aud/exp + scope)
```

The service does not care *who* signs the JWT — that is pure config
(`OIDC_ISSUER` / `OIDC_JWKS_URL` / `OIDC_AUDIENCE`):

- **local dev**: point it straight at your identity provider (no gateway).
- **prod**: point it at Apigee's issuer/JWKS (Apigee mints the internal JWT).

Edge concerns — TLS, CORS, rate limiting, quotas, spike arrest — are owned by the
gateway and are deliberately not implemented here.

## Authentication vs authorization

- **Authn** (is the token genuine?): signature verified against the issuer's
  JWKS (cached, refreshed on rotation), plus `iss` / `aud` / `exp` / `nbf` with
  clock-skew leeway. Failure → `401` with `WWW-Authenticate: Bearer`.
- **Authz** (is the caller allowed here?): the required scope must be present in
  the token's `scope` claim. Failure → `403`.

## Scopes and adding future endpoints

Scopes are standard OAuth2 (space-delimited `scope` claim). Who *gets* a scope is
decided by the IdP admin when the client is registered; the API only *enforces*
the scope an endpoint declares.

Adding a scoped endpoint is two explicit steps:

1. **Grant the scope** to the relevant client(s) in your IdP / Apigee policy.
2. **Guard the route in code** with the reusable factory in `src/api/deps.py`:

   ```python
   require_cancel = require_scope(lambda _s: "jobs.cancel")   # literal, or
   require_cancel = require_scope(lambda s: s.scope_cancel)   # env-named

   @router.post("/v1/jobs/{job_id}/cancel")
   def cancel(job_id: int, principal: Principal = Depends(require_cancel)):
       ...
   ```

The API never treats an arbitrary scope in a token as permission for an
arbitrary endpoint — the endpoint → scope mapping is always explicit in code.
Scope *names* are env-overridable (`SCOPE_READ`, `SCOPE_WRITE`, ...) so ops can
align them to whatever the IdP mints, but the *policy* lives in code, not in
environment variables.

## Store selection (demo → prod)

The app selects its store from `DATABASE_URL`:

- **unset** → `InMemoryStore` (process-local; for demos and tests, no infra).
- **set** → `PostgresStore` takes over with no code change; enqueue writes the
  job and its outbox row in one transaction against the shared schema.

## Project layout

```
src/
  app.py        FastAPI application factory
  config.py     Settings (from env)
  errors.py     RFC 7807 problem responses + handlers
  main.py       uvicorn entrypoint
  api/          routes, schemas, dependencies, middleware
  auth/         JWT verifier + Principal
  store/        Store protocol, in-memory + Postgres backends
  domain/       Job model, statuses, outbox envelope
tests/          unit/integration tests (JWT path exercised with locally-minted keys)
```

## Configuration

| Env var                 | Default       | Meaning                                             |
|-------------------------|---------------|-----------------------------------------------------|
| `DATABASE_URL`          | *(unset)*     | Unset → in-memory store; set → Postgres.            |
| `OIDC_ISSUER`           | `""`          | Expected `iss`; signer of the JWT the service gets. |
| `OIDC_AUDIENCE`         | `""`          | Expected `aud`.                                     |
| `OIDC_JWKS_URL`         | `""`          | JWKS URL; empty → discovered from the issuer.       |
| `OIDC_CLOCK_SKEW_LEEWAY`| `60`          | Seconds of leeway on `exp`/`nbf`/`iat`.             |
| `OIDC_JWKS_CACHE_TTL`   | `3600`        | JWKS cache TTL (seconds).                           |
| `SCOPE_WRITE`           | `jobs.write`  | Scope required by `POST /v1/jobs`.                  |
| `SCOPE_READ`            | `jobs.read`   | Scope required by `GET /v1/jobs/{id}`.              |
| `HOST` / `PORT`         | `0.0.0.0`/`8080` | Bind address.                                    |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check .          # lint
pytest                # tests + coverage gate (--cov-fail-under=90)
```

Run locally (with your IdP config in the environment):

```bash
PYTHONPATH=src OIDC_ISSUER=... OIDC_AUDIENCE=... OIDC_JWKS_URL=... python -m main
```

Or containerized:

```bash
docker build -t job-api .
docker run --rm -p 8080:8080 \
  -e OIDC_ISSUER=... -e OIDC_AUDIENCE=... -e OIDC_JWKS_URL=... \
  job-api
```

### Example requests

```bash
curl -sS -X POST localhost:8080/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "hello", "payload": {"name": "Ada"}}'

curl -sS localhost:8080/v1/jobs/1 -H "Authorization: Bearer $TOKEN"
```
