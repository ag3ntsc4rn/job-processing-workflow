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
| `worker/` | worker | consume Kafka, claim (compare-and-set), run the job-type handler |
| `reaper/` | reaper | recover jobs stuck in `running` (per-type timeout + churn cap) |
| `migrations/` | schema | idempotent DDL |

**Adding a new job type is a worker-only change**: add a handler in
`worker/handlers/` decorated with `@register("your_type")`. The handler,
dispatcher, and reaper are all type-agnostic (`job_type` is just data).

## Run it (docker compose)

```bash
docker compose up --build -d          # postgres, kafka, migrate, dispatcher, worker, reaper
docker compose run --rm handler hello '{"name":"Ada"}'   # enqueue a job
docker compose logs -f worker         # watch it get processed
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
