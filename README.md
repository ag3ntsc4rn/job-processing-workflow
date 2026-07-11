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
