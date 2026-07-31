# job-enqueue-client

Standalone producer for the job orchestrator: name a `job_type`, optionally pass
a JSON payload of overrides, and it `POST`s them to the jobs API.

```bash
pip install -r requirements.txt
python main.py settlement '{"region": "eu"}'
```

```
python main.py <job_type> [payload_json]
  --api-url URL        base URL of the jobs API      (env: JOB_API_URL)
  --scope SCOPE        scope to mint, repeatable     (env: JOB_API_SCOPES)
  --max-attempts N     attempts incl. the first      (env: JOB_API_MAX_ATTEMPTS)
```

Exit codes: `0` enqueued **or** duplicate (`409` — an active job of that type
already exists, which is a normal outcome, not a failure), `1` the API rejected
the request or every attempt failed, `2` bad usage/config.

## What it sends

```
POST {JOB_API_URL}/v1/jobs
Authorization: Bearer <locally minted JWT>
Content-Type: application/json

{"job_type": "settlement", "payload": {"region": "eu"}}
```

`payload` is omitted entirely when no payload is given. The payload is a set of
**overrides**: the job type's stored base config supplies the defaults and the
worker overlays this input on top of it (input wins per key).

## Auth: minted JWT today, OAuth2 later

There is **no OAuth2 flow here**. `jobclient.tokens.mint_jwt` locally signs a JWT
(HS256, placeholder secret) carrying `iss`/`sub`/`aud`/`iat`/`nbf`/`exp`/`jti` and
a space-delimited `scope` claim — the same shape the API will receive from
Apigee, so the wire format doesn't change when real tokens arrive. **Nothing
verifies this signature**; it exists so the caller can state its scopes.

When the Apigee client id/secret exist, replace `mint_jwt` with a
client-credentials fetch — it has exactly one call site, in
`JobClient.enqueue`, and everything else (retries, exit codes, output) is
unaffected.

## Retries

Connection errors, timeouts, and transient statuses (`408`, `425`, `429`, `500`,
`502`, `503`, `504`) are retried with exponential backoff and full jitter, capped
at `JOB_API_BACKOFF_MAX`; a server-sent `Retry-After` (delta-seconds) wins over
the computed delay. Every other status — including `409` and `4xx` authz errors —
returns immediately, since retrying it cannot change the answer.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `JOB_API_URL` | `http://localhost:8080` | Base URL of the jobs API. |
| `JOB_API_JOBS_PATH` | `/v1/jobs` | Enqueue path appended to the base URL. |
| `JOB_API_SCOPES` | `jobs.write` | Space-delimited scopes put in the token. |
| `JOB_API_TOKEN_ISSUER` | `job-enqueue-client` | `iss` claim. |
| `JOB_API_TOKEN_AUDIENCE` | `job-api` | `aud` claim. |
| `JOB_API_TOKEN_SUBJECT` | `job-enqueue-client` | `sub` claim — who is enqueuing. |
| `JOB_API_TOKEN_ALGORITHM` | `HS256` | Signing algorithm. |
| `JOB_API_TOKEN_SECRET` | placeholder | Signing key. Unverified today. |
| `JOB_API_TOKEN_TTL` | `300` | Token lifetime in seconds. |
| `JOB_API_TIMEOUT` | `10` | Per-request timeout in seconds. |
| `JOB_API_MAX_ATTEMPTS` | `4` | Total attempts including the first. |
| `JOB_API_BACKOFF_INITIAL` | `0.5` | First backoff ceiling in seconds. |
| `JOB_API_BACKOFF_MAX` | `8` | Backoff ceiling cap in seconds. |

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest            # unit tests over an in-process transport; 90% coverage gate
```

This directory is self-contained: copying it to the root of its own repo is the
whole migration (GitHub only reads workflows from a repo root, so
`.github/workflows/ci.yml` runs once this is a repo of its own).

CI (`.github/workflows/ci.yml`) runs lint + tests with a coverage gate and PR
comment, `pip-audit`, and a smoke job that drives the **real CLI over real HTTP**
against `tests/stub_api.py` — asserting `201` then `409`, that the scopes reach
the server, and that two `503`s are retried into a success. Run it locally:

```bash
python tests/stub_api.py --port 8099 &
JOB_API_URL=http://localhost:8099 python main.py settlement '{"region":"eu"}'
```
