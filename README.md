# job-api

A small FastAPI service for enqueuing and reading jobs, built to run **behind an
API gateway (Apigee)**. In production it **re-verifies** the JWT it receives
(signature + `iss`/`aud`/`exp` against the issuer's JWKS) and enforces
per-endpoint OAuth2 scopes; in local dev it can skip verification so you can
craft your own token. It is machine-to-machine only.

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
Apigee   --(Bearer JWT)--------> job-api: verify sig/iss/aud/exp (JWKS)
                                   -> routes (read claims + enforce scope)
```

Edge concerns — TLS, CORS, rate limiting, quotas, spike arrest — are owned by the
gateway and are deliberately not implemented here.

### Why the API re-verifies (defense-in-depth)

"Trusting the gateway" is only safe if the gateway is the *only* network path to
the service. If the API is directly reachable, a caller could bypass Apigee and
present a token. Re-verifying the signature against the issuer's JWKS means a
**forged / self-minted** token is rejected with `401` even on a direct call —
an attacker can't produce a valid signature without the issuer's private key.
(Signature verification proves the token is *genuine*; it does not prove the
request came through Apigee — for that you still need network isolation / mTLS /
a gateway-shared-secret header.)

## Auth modes (`AUTH_VERIFY`)

| `AUTH_VERIFY` | Use        | Behavior                                                              |
|---------------|------------|-----------------------------------------------------------------------|
| `true` (default) | prod    | Re-verify signature + `iss`/`aud`/`exp` via JWKS. Forged/expired/wrong-audience token → `401`. Requires OIDC config. |
| `false`       | local dev  | No signature check — claims read straight from the token payload, so you can craft your own token. Startup logs a loud warning. |

Either mode still **requires** a Bearer token (missing/unreadable → `401` with
`WWW-Authenticate: Bearer`) and runs the same scope authorization.

## Authentication vs authorization

- **Authn** (is the token genuine?): when `AUTH_VERIFY` is on, the service checks
  signature + `iss`/`aud`/`exp` against the issuer's JWKS; when off, it trusts
  the payload (dev only). Missing/unreadable Bearer token → `401`.
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
  config.py     Settings (from env)
  errors.py     RFC 7807 problem responses + handlers
  main.py       app factory (create_app + module-level `app`) + uvicorn entrypoint
  api/          routes, schemas, dependencies (claim reading + scope guards), middleware
  auth/         Principal (claims -> service identity) + JWT verifier (JWKS)
  store/        Store protocol, in-memory + Postgres backends
  domain/       Job model, statuses, outbox envelope
tests/          unit/integration tests (scopes/claims + JWKS verify path)
```

`get_principal` in `src/api/deps.py` picks the mode: if a verifier was built
(`AUTH_VERIFY` on) it calls `verifier.verify(token)`; otherwise it reads claims
from the token payload. The verifier is built once at startup and stashed on
`app.state.verifier`.

## Configuration

| Env var                 | Default       | Meaning                                             |
|-------------------------|---------------|-----------------------------------------------------|
| `DATABASE_URL`          | *(unset)*     | Unset → in-memory store; set → Postgres.            |
| `AUTH_VERIFY`           | `true`        | `true` → re-verify JWT via JWKS (prod); `false` → dev, no signature check. |
| `OIDC_ISSUER`           | *(unset)*     | Expected `iss`; used for JWKS discovery if `OIDC_JWKS_URL` unset. Required when verifying. |
| `OIDC_AUDIENCE`         | *(unset)*     | Expected `aud`. Required when verifying.            |
| `OIDC_JWKS_URL`         | *(unset)*     | Issuer JWKS endpoint; if unset it's discovered from `OIDC_ISSUER`. |
| `OIDC_CLOCK_SKEW_LEEWAY`| `60`          | Seconds of clock skew tolerated on `exp`/`nbf`.     |
| `OIDC_JWKS_CACHE_TTL`   | `3600`        | Seconds to cache JWKS before refreshing.            |
| `SCOPE_WRITE`           | `jobs.write`  | Scope required by `POST /v1/jobs`.                  |
| `SCOPE_READ`            | `jobs.read`   | Scope required by `GET /v1/jobs/{id}`.              |
| `HOST` / `PORT`         | `0.0.0.0`/`8080` | Bind address.                                    |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .      # puts src/ on the import path (editable install)

ruff check .          # lint
pytest                # tests + coverage gate (--cov-fail-under=90)
```

The editable install (`pip install -e .`) is what makes `import config`,
`import api...`, and `uvicorn main:app` resolve from anywhere — no `PYTHONPATH`
needed. (Editors: `src` is declared as a source root via `[tool.pyright]` /
`.vscode/settings.json`.)

Run locally in **dev mode** (no OIDC config, craft your own token):

```bash
export AUTH_VERIFY=false
export PYTHONPATH=src
cd src
python -m uvicorn main:app --host 0.0.0.0 --port 8080

# equivalently, honouring HOST/PORT from the env:
python -m main
```

Mint a dev token (the signing key is irrelevant — nothing checks it in dev):

```bash
TOKEN=$(python -c "import jwt; print(jwt.encode({'sub':'dev','client_id':'dev-app','scope':'jobs.read jobs.write'}, 'x'*32, algorithm='HS256'))")
```

Drop `jobs.write` from the scope → `POST` returns `403`; omit the header → `401`.

In **prod** leave `AUTH_VERIFY` at its default (`true`) and set `OIDC_ISSUER` /
`OIDC_AUDIENCE` (and optionally `OIDC_JWKS_URL`) to the gateway/IdP that signs
the tokens. With the editable install you can run `uvicorn main:app` from
anywhere without `PYTHONPATH`.

Or containerized:

```bash
docker build -t job-api .
docker run --rm -p 8080:8080 -e AUTH_VERIFY=false job-api   # demo; drop the env in prod + set OIDC_*
```

### Example requests

```bash
curl -sS -X POST localhost:8080/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "hello", "payload": {"name": "Ada"}}'

curl -sS localhost:8080/v1/jobs/1 -H "Authorization: Bearer $TOKEN"
```
