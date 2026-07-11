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
| `worker/` | worker | consume Kafka, claim (compare-and-set), snapshot config payload, run the job-type handler |
| `reaper/` | reaper | recover jobs stuck in `running` (per-type timeout + churn cap) |
| `migrations/` | schema | idempotent DDL |

**Adding a new job type is a worker-only change**: add a handler in
`worker/handlers/` decorated with `@register("your_type")`, plus one
`job_type_config` row (its `payload`, `run_timeout`, `max_attempts`). The
handler, dispatcher, and reaper are all type-agnostic (`job_type` is just data).

**The message envelope is a pure pointer** — `{job_id, job_type}`. Producers
(AutoSys/handler) only *name* the job; the business payload lives in
`job_type_config` and the worker snapshots it into `jobs.payload` when it claims
the run (so each run records the exact inputs it used). A type with no config
row is un-runnable: the worker fails such a run gracefully.

## Run it (docker compose)

```bash
docker compose up --build -d          # postgres, kafka, migrate, dispatcher, worker, reaper
docker compose run --rm handler hello   # enqueue a job (payload comes from job_type_config)
docker compose logs -f worker         # watch it get processed
```

The `migrate` step seeds `job_type_config` rows for the demo types `hello`
(`{"name": "Ada"}`) and `boom` (`{}`). To add/override a type's payload:

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
