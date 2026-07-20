# job-api

A small FastAPI service for enqueuing and reading jobs, built to run **behind an
API gateway (Apigee)**. The JWT is validated **upstream** by an enterprise JWT
auth middleware (wired in `main.py` via `add_jwt_auth(app, exclude_routes=[...])`);
this service reads the already-validated claims and enforces per-endpoint OAuth2
scopes. It is machine-to-machine only.

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
Apigee   --(Bearer JWT)--------> JWT auth middleware (validates sig/iss/aud/exp)
                                   -> job-api routes (read claims + enforce scope)
```

Token authentication (signature / `iss` / `aud` / `exp`) is handled by the
enterprise **JWT auth middleware** mounted in `main.py`, so this service holds no
OIDC/JWKS configuration. By the time a request reaches a route the token is
already validated; the route only reads the claims and checks scopes.

Edge concerns — TLS, CORS, rate limiting, quotas, spike arrest — are owned by the
gateway and are deliberately not implemented here.

## Authentication vs authorization

- **Authn** (is the token genuine?): done **upstream** by the enterprise JWT auth
  middleware — signature, `iss`, `aud`, `exp`. This service does not re-verify
  it; it just requires validated claims to be present (a missing/unreadable
  Bearer token → `401` with `WWW-Authenticate: Bearer`).
- **Authz** (is the caller allowed here?): the required scope must be present in
  the token's `scope` claim. Failure → `403`. This is *not* something a generic
  auth middleware does, so it lives here.

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
  auth/         Principal (validated claims -> service identity)
  store/        Store protocol, in-memory + Postgres backends
  domain/       Job model, statuses, outbox envelope
tests/          unit/integration tests (scopes/claims; auth middleware is out of scope)
```

### Wiring the auth middleware

`create_app` in `src/main.py` has a marked spot to mount your enterprise
middleware, e.g.:

```python
from your_company.auth import add_jwt_auth
add_jwt_auth(app, exclude_routes=["/healthz", "/readyz", "/docs", "/openapi.json"])
```

Routes read the validated claims via `get_principal` in `src/api/deps.py`. By
default it decodes the Bearer payload (signature already verified upstream); if
your middleware exposes decoded claims directly (e.g. `request.state.claims`),
swap the one-line body of `_claims_from_request` to read from there.

## Configuration

| Env var                 | Default       | Meaning                                             |
|-------------------------|---------------|-----------------------------------------------------|
| `DATABASE_URL`          | *(unset)*     | Unset → in-memory store; set → Postgres.            |
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

Run locally — no OIDC config needed:

```bash
export PYTHONPATH=src
cd src
python -m uvicorn main:app --host 0.0.0.0 --port 8080

# equivalently, honouring HOST/PORT from the env:
python -m main
```

With the editable install you can also run `uvicorn main:app` from anywhere
without `PYTHONPATH`.

Or containerized:

```bash
docker build -t job-api .
docker run --rm -p 8080:8080 job-api
```

### Example requests

```bash
curl -sS -X POST localhost:8080/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "hello", "payload": {"name": "Ada"}}'

curl -sS localhost:8080/v1/jobs/1 -H "Authorization: Bearer $TOKEN"
```
