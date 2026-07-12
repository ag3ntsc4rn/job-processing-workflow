# Resilient Job Orchestration — Simple Version

Any producer → Enqueue step (insert if not active) → Dispatcher (DB→Kafka) → Workers (process + report).
Postgres is the source of truth; Kafka is transport. A little latency is fine, so everything is poll-based.

**The queue is source-agnostic.** AutoSys firing a job handler is just *one* producer. The contract for getting work into the system is a single thing: *insert a `jobs` row (+ `outbox` row) in one transaction*. Anything that can do that insert is a valid producer — an AutoSys-triggered handler, a REST endpoint, another service reacting to an event, a manual backfill script, a cron. None of the downstream pieces (dispatcher, workers) know or care where the row came from. So "job handler" below means "whatever enqueued the row," not a specific component.

---

## What I'm dropping from the earlier draft

- **`priority`** — gone. FIFO by `created_at`. Only add it if some job types must jump the queue.
- **`dedup_key`** — gone. Your rule is "one active job **per job_type**", so the key *is* `job_type`. No separate business key. (See below for when you'd ever want one.)
- **`attempts` / `max_attempts` / backoff** — **not used.** We chose Option A (§5): a failure just marks the job `failed`, and the next AutoSys schedule enqueues a fresh job. No retry counter, no backoff, no delay topics. (If you ever want in-system retries later, §5 Option B shows the small addition — but it's not part of this design.)

---

## 1. The `jobs` table (minimal)

```sql
CREATE TABLE jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued',  -- queued|dispatched|running|completed|failed
    input_payload JSONB     NOT NULL DEFAULT '{}'::jsonb, -- optional per-run overrides the producer sent (audit of the request)
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- effective config the run used = base config || input, snapshotted at claim
    attempts    INT         NOT NULL DEFAULT 0,          -- times this run has been (re)dispatched; for churn cap (§7)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce "at most one active job per job_type" in the DB itself.
-- "Active" = every non-terminal state: queued, dispatched, AND running.
CREATE UNIQUE INDEX jobs_one_active_per_type
    ON jobs (job_type)
    WHERE status IN ('queued', 'dispatched', 'running');
```

Per-`job_type` config lives in a tiny lookup table (one row per type) — the **base payload** for the type plus its stuck-run timeout and churn cap — so none of it is hard-coded and it works whether or not a producer sends anything:

```sql
CREATE TABLE job_type_config (
    job_type      TEXT PRIMARY KEY,
    payload       JSONB    NOT NULL DEFAULT '{}'::jsonb, -- base config: the master set of keys with defaults for this type
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',   -- how long a run may sit in 'running' before it's stuck
    max_attempts  INT      NOT NULL DEFAULT 3           -- reclaim cap before dead-letter (§7)
);
```

> `attempts` here is **not** business retries (that's still Option A). It only counts how many times the *reaper* has re-dispatched a stuck run, so a poison job can't loop forever.

> **Base config + optional per-run overrides.** Every `job_type` has a **base config** in `job_type_config.payload` — the master set of keys with defaults. A producer (AutoSys or anything else) *may* pass an optional per-run JSON payload, which is stored in `jobs.input_payload` (durable audit of exactly what was requested). When the worker claims the run it computes the **effective payload** = `base_config` overlaid with `input_payload`, where **input keys win** per key, and snapshots that into `jobs.payload`. So a run with no producer payload runs on pure defaults; a run that sends `{"batch_size": 25}` overrides just that key and inherits the rest. The merge is a **shallow** object merge (top-level keys; nested objects are replaced wholesale, matching Postgres `||`). The override rides in the DB row, **never on the Kafka message** — the envelope stays a pure pointer. A type with **no** config row is un-runnable — the worker fails such a run gracefully rather than leaving the dedup slot occupied.

Plus a tiny **outbox** table so the handler never has to write to the DB and Kafka in the same breath (§3):

```sql
CREATE TABLE outbox (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id),
    payload      JSONB       NOT NULL,          -- envelope: {job_id, job_type}
    published_at TIMESTAMPTZ                    -- NULL until the dispatcher sends it
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
```

That's the whole schema (plus `outbox`). No separate executions/history table — `jobs` rows are never overwritten, so the table doubles as the durable per-run history (§4).

---

## 2. Why the dedup check needs the DB, not app code

Your requirement: *"the handler checks if the same job is queued or running; if so, don't queue another."*

The naive way is:
```
rows = SELECT ... WHERE job_type=? AND status IN ('queued','dispatched','running')
if not rows: INSERT ...
```
This has a **race**: two AutoSys triggers (or two handler instances) both run the SELECT, both see zero rows, both INSERT → two active jobs. The check-then-insert is not atomic.

The **partial unique index** above makes the database enforce the rule. The handler just does:
```sql
INSERT INTO jobs (job_type) VALUES (?)
ON CONFLICT DO NOTHING;   -- 0 rows inserted = one is already active, so skip
```
No SELECT, no race, correct no matter how many handlers run. **This is the whole point** — it's not extra machinery, it *replaces* your check-if-queued-or-running logic with one line the DB can't get wrong.

### When would you ever need a "dedup key"?
Only if you wanted **multiple active jobs of the same type at once**, distinguished by something — e.g. one `settlement` job per *account*, so `settlement/acct-123` and `settlement/acct-456` can both run, but not two of `acct-123`. Then the unique index would be on `(job_type, account_id)`. **Since your rule is one-active-per-type, you don't need it.** I only mentioned it because it's the same mechanism generalized.

---

## 3. Flow & statuses

```
queued ──► running ──► completed
                │
                └──► failed
```

**Handler** — one transaction, two inserts (the outbox row is what the dispatcher will send):
```sql
BEGIN;
  INSERT INTO jobs (job_type, input_payload) VALUES (?, ?)
    ON CONFLICT DO NOTHING            -- 0 rows = already active, skip
    RETURNING id;                     -- input_payload defaults to '{}' when the producer sends nothing
  -- only if a row was inserted (outbox payload is the pointer envelope, no business data):
  INSERT INTO outbox (job_id, payload) VALUES (?, ?);
COMMIT;
```
Because both inserts are in the same transaction, a Kafka message will exist **iff** the job was durably queued — no dual-write hole. Handler only touches Postgres, so AutoSys gets a fast ack.

**Dispatcher** — drains the outbox, publishes, marks sent. Run one or many; `SKIP LOCKED` makes it safe:
```sql
SELECT id, job_id, payload FROM outbox
WHERE published_at IS NULL
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 100;
-- for each: produce to Kafka, then
--   UPDATE outbox SET published_at = now() WHERE id = ?;
--   UPDATE jobs   SET status = 'running'  WHERE id = ?;   -- (optional intermediate state)
```
Crash before publish → row stays `published_at IS NULL` → retried next loop (at-least-once). Crash after publish but before the UPDATE → message re-sent → duplicate, absorbed by the worker guard in §6.

**Worker** — consumes from Kafka, claims the run, **resolves the effective payload** (`job_type_config.payload` overlaid with the run's `input_payload`, input keys winning) and snapshots it into `jobs.payload` (recording the exact config this run used; a type with no config row is failed gracefully), does the work, and sets `completed` or `failed`. In Postgres the merge is one statement: `SET payload = c.payload || j.input_payload`.

---

## 4. No separate executions table — `jobs` *is* the history

**Decision:** dropped `job_executions` entirely. The `jobs` table already gives us everything it would have, because rows are never overwritten across runs:

- **Every run is its own row.** The partial unique index (§1) only blocks a *second active* row per `job_type` — it does **not** delete or reuse the old one. So each successful queue creates a new row, and completed/failed rows stay in the table forever. Over time `jobs` naturally accumulates one row per run per `job_type` — that *is* the run history.
- **Timings come from the row itself.** `created_at` (queued), `updated_at` (last transition), and `status` tell you what happened to each run and roughly when. "Show me every `settlement` run this month and how each ended" is a single `SELECT ... WHERE job_type='settlement' ORDER BY created_at`.
- **`jobs.id` is the run/correlation id.** It's auto-generated and unique per row = unique per run, so the worker just stamps `jobs.id` on every log line it ships to the SIEM. From any DB row you pivot straight to that run's full step-by-step timeline in the SIEM. No second table needed to link them.
- **Claim guard lives on `jobs` too.** The worker claims a redelivered-safe run with a compare-and-set: `UPDATE jobs SET status='running' WHERE id=? AND status IN ('queued','dispatched')`. Only one worker's UPDATE matches; a duplicate delivery gets `0 rows` and simply acks and skips (§6). No `UNIQUE(job_id)` on a separate table required. Right after winning the claim, the worker resolves `job_type_config.payload || jobs.input_payload` and snapshots it into `jobs.payload` so the run records the exact config it used. (Accepting `queued` as well as `dispatched` matters: the dispatcher publishes to Kafka *before* it marks the row `dispatched`, so a fast worker can receive the message while the row is still `queued` — requiring only `dispatched` would strand it.)

So the division of labor is dead simple: **`jobs`** = current state *and* durable per-run history (+ the run_id for SIEM), and **the SIEM** = the detailed step-by-step narrative. The `outbox` is the only other table.

> If you ever needed *multiple* runs to share one logical identity (e.g. Option B retries reusing a single row, or a parent/child job graph), you'd reintroduce a separate executions/runs table then. For Option A with one-active-per-type, `jobs` covers it.

---

## 5. Retries — decision: Option A (AutoSys re-trigger)

**Chosen:** Option A. A failed job stays `failed`; the next AutoSys schedule enqueues a fresh one. Nothing else in the design changes and no retry columns are needed. Option B is documented below only as a future path if fast in-system retries ever become necessary.

There are two independent ways a failed job can run again, and they solve different problems. You can use either, or both.

### Option A — let AutoSys re-trigger on the next schedule
The job just goes to `failed` and does nothing else. The next time AutoSys fires (next day / next cycle), the producer enqueues a fresh job.

- **Pros:** zero extra machinery. No `attempts` column, no retry loop. The schedule *is* the retry policy, and operators already understand it.
- **Cons:** recovery is only as fast as the schedule — a job that failed at 00:05 on a transient blip waits until tomorrow. And it retries *everything* the same way, whether the cause was a 2-second network hiccup or a genuinely broken input.
- **Best when:** jobs are naturally periodic (daily batches), failures are rare, and waiting a full cycle is acceptable. Also note the source-agnostic point up top — if a producer *isn't* on a schedule (a one-off REST enqueue), there's nothing to "re-trigger," so Option A doesn't cover it.

### Option B — the system retries transient failures itself
Distinguish two failure kinds in the worker:
- **Transient** — likely to succeed if tried again: DB deadlock/timeout, network blip, a dependency returning 5xx / "try later." → worth retrying automatically.
- **Permanent** — will fail identically every time: bad/validation-failing payload, 4xx, a business rule violation. → do **not** retry; go straight to `failed` and surface it. Retrying these just burns cycles and hides the real problem.

Smallest implementation:
- add `attempts INT DEFAULT 0` to `jobs`;
- on a **transient** failure, set status back to `queued` (the dispatcher re-publishes it) and increment `attempts`, until `attempts` hits a cap (say 3–5), after which leave it `failed`;
- on a **permanent** failure, go to `failed` immediately regardless of `attempts`.

No delay topics, no separate backoff table — just a counter and reusing the existing `queued` → dispatcher path. If you want spacing between attempts, add an `available_at TIMESTAMPTZ` and have the dispatcher ignore rows whose `available_at` is in the future; set it to `now() + backoff` on each retry. That's the only addition, and it's optional.

### How they combine
They layer cleanly: Option B absorbs the short, self-healing failures within minutes (so a transient blip doesn't wait a whole day), and Option A is the backstop — if a job exhausts its retries and lands in `failed`, the next scheduled AutoSys run still gives it a fresh shot. A reasonable default: **B with a small cap for fast transient recovery, A as the daily safety net.** If you truly want the simplest possible system and daily latency on failures is fine, start with A alone and add B later — nothing else in the design changes.

**One caution:** whichever you pick, a retried/re-triggered job can mean the work runs more than once, so the idempotency guard in §6 matters. (With Option A the dedup index also prevents a re-trigger from stacking a second active job while one is still running.)

---

## 6. The one real gotcha: duplicate delivery

Kafka is at-least-once — a worker can occasionally see the same message twice (e.g. it processed the job but crashed before acking, so Kafka redelivers). Two cheap guards:
- Worker only transitions `running → completed` for a job that's still `running` (guarded UPDATE), so a duplicate no-ops.
- Where practical, make the actual work idempotent (upserts / "already done?" checks).

This is the only place duplicates can arise, and it's inherent to any queue — worth one sentence of awareness, not a lot of machinery.

---

## 7. Stuck-in-`running` recovery (per-type timeout + churn cap)

If a worker dies mid-run, the row is left in `running`. Because `running` is an *active* status, the dedup index then blocks **every future trigger for that `job_type`** — so recovery is mandatory, not optional. One small periodic **reaper** handles it.

**Detect** — a run is stuck if it's been `running` longer than *its type's* timeout (from `job_type_config`, not a global constant):

```sql
SELECT j.id, j.attempts, c.max_attempts
FROM jobs j
JOIN job_type_config c USING (job_type)
WHERE j.status = 'running'
  AND j.updated_at < now() - c.run_timeout;
```

**Recover** — for each stuck row, one transaction. Either re-dispatch it (if under the cap) or park it:

```sql
-- under the cap: re-queue + re-arm the outbox so the dispatcher publishes a fresh message
BEGIN
  UPDATE jobs SET status='queued', attempts=attempts+1, updated_at=now()
    WHERE id=:id AND status='running' AND attempts < :max_attempts;
  UPDATE outbox SET published_at=NULL WHERE job_id=:id;   -- re-arm (or INSERT a fresh row)
COMMIT

-- cap reached: give up, go terminal, and let go of the dedup slot
UPDATE jobs SET status='failed', updated_at=now()
  WHERE id=:id AND status='running' AND attempts >= :max_attempts;
```

That's the whole mechanism:
- **Per-type timeout** — `run_timeout` per `job_type` means a 10-second job and a 2-hour batch each get a sensible "stuck" threshold instead of one blunt global value.
- **Churn cap** — `attempts` counts reaper re-dispatches; once it hits `max_attempts`, the row goes to terminal `failed` instead of looping forever. A poison job now surfaces as a visible failure (alert on it) rather than silently cycling.
- Going terminal also **frees the dedup slot**, so the next AutoSys schedule can enqueue a fresh run of that type.

Keep it simple: a single query loop on a timer (say every minute). No leases, no heartbeats — those are a later upgrade only if fixed per-type timeouts prove too blunt. As always, a reaped-but-actually-alive worker means the work can run twice, so idempotent work (§6) is what keeps that safe.

---

## 8. Components, repos & replicas

Four deployable components + a shared contract library. The organizing principle: **`job_type` is data everywhere except the worker**, so adding a new job type is a worker-only change.

| Repo / component | Job-type-aware? | Replicas | Notes |
|---|---|---|---|
| **handler** | No | **2+** | Stateless; `INSERT`s `jobs`+`outbox` with `job_type` as a pass-through string. Partial unique index makes concurrent instances safe. (If AutoSys spawns a process per trigger, this is just N safe concurrent invocations.) |
| **dispatcher** | No | **2** | Drains `outbox` → Kafka. `FOR UPDATE SKIP LOCKED` means 2 active instances never double-publish, so run 2 for HA (not active/standby). |
| **worker** | **Yes** | **= topic partition count** (e.g. 4) | The only type-aware component. One service consuming one topic; a registry/plugin dispatches by `job_type`. This is the horizontal scale knob — max useful concurrency per group = partition count. |
| **reaper** | No | **1** | Periodic stuck-`running` sweep (~1 min). Idempotent, needs no HA; can be a `pg_cron`/scheduled task or folded into the dispatcher. |
| *(shared contract lib)* | — | — | Versioned schema + message envelope, depended on by the others. |

**Infra (not components you write):** Postgres (1 primary, optional replica for HA/reads) and Kafka (partition count on the `jobs` topic caps worker parallelism).

**Suggested starting point:** handler ×2 · dispatcher ×2 · workers ×4 (= 4 partitions) · reaper as a 1-min scheduled job · Postgres primary · Kafka `jobs` topic with ~4 partitions. Scale by raising partitions **and** workers together as peak volume grows.

### Adding a new job type = deploy the worker only

Because `job_type` is data, three of the four components never change:

- **handler** — `INSERT INTO jobs(job_type, input_payload)`; `job_type` is passed straight through, no `switch`. It never *interprets* the payload — it just stores the producer's optional overrides for the worker to merge later.
- **dispatcher** — copies `outbox` bytes to Kafka; never inspects the payload.
- **reaper** — acts on `status`/timeout; per-type timeouts come from the `job_type_config` **table**, not code.
- **worker** — the *only* place type logic lives. A registry (like a `@register("my_type")` decorator) maps `job_type` → a handler class. New type = add one handler, register it, deploy the worker.

The one non-code step is a `job_type_config` **row** (`payload`, `run_timeout`, `max_attempts`) — a data insert, not a redeploy. `run_timeout`/`max_attempts` fall back to defaults when absent, but the **base payload** must be present: it's the master set of keys with defaults, so a type with no config row has nothing to run and the worker fails such a run gracefully.

**Contract to hold the line:** every type shares one **message envelope** (`{job_id, job_type}` — a pure pointer; base config lives in `job_type_config`, per-run overrides in `jobs.input_payload`) on one **topic**. As long as that's stable, the generic components never redeploy. A type needing a different envelope or its own topic/ordering is the only thing that would touch the dispatcher/contract — so agree on a stable envelope up front.

### How other processes enqueue (without duplicating the insert)

AutoSys is just one producer; the queue is source-agnostic (see the intro). The one thing every producer must do identically is *the enqueue transaction* — insert `jobs` + `outbox` atomically and let the partial unique index handle dedup. That logic must live in **exactly one place** so it can't drift. In this repo that place is `handler.service.enqueue(store, job_type, payload=None)`. Producers reuse it three ways, none of which re-implement the SQL:

- **Import it** — an in-process/Python producer depends on the shared package and calls `enqueue(...)` directly (this is the "shared contract lib" from the multi-repo topology).
- **Call it over HTTP** — wrap `enqueue` in a thin endpoint (`POST /jobs {job_type, payload}`) for producers that aren't Python or shouldn't hold DB credentials. The handler service owns the DB; callers just POST.
- **Shell out** — `python -m handler <job_type> [payload_json]`, which is what an AutoSys job definition invokes.

The anti-pattern to avoid is a producer hand-writing its own `INSERT INTO jobs ...` — that duplicates the jobs+outbox+dedup invariant and is where correctness drifts over time. Direct SQL is acceptable only as a deliberate, versioned dependency on the schema, not as the default.

---

## 9. Appendix — final schema & all queries

Everything in one place, consolidated from the sections above.

### 9.1 Schema (DDL)

```sql
-- Current state + durable per-run history (rows are never overwritten).
CREATE TABLE jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued',  -- queued|dispatched|running|completed|failed
    input_payload JSONB     NOT NULL DEFAULT '{}'::jsonb, -- optional per-run overrides the producer sent (audit of the request)
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- effective config the run used = base config || input, snapshotted at claim
    attempts    INT         NOT NULL DEFAULT 0,          -- reaper re-dispatch count (churn cap), NOT business retries
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- who enqueued it, from the HTTP API's validated OIDC token; NULL for the
    -- CLI / direct producers (migration 004). job_id stays the SIEM correlation id.
    created_by_sub    TEXT,   -- token `sub` (stable user/service id)
    created_by_type   TEXT,   -- 'user' | 'service'
    created_by_client TEXT    -- calling app's client_id / azp
);
CREATE INDEX jobs_created_by_sub_idx ON jobs (created_by_sub);

-- At most one ACTIVE job per job_type. "Active" = every non-terminal status.
CREATE UNIQUE INDEX jobs_one_active_per_type
    ON jobs (job_type)
    WHERE status IN ('queued', 'dispatched', 'running');

-- Helps the dispatcher's status scans and history queries.
CREATE INDEX jobs_status_idx  ON jobs (status);
CREATE INDEX jobs_type_created_idx ON jobs (job_type, created_at DESC);

-- Transactional outbox: handler writes it in the same tx as the job row;
-- dispatcher drains it to Kafka.
CREATE TABLE outbox (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id),
    payload      JSONB       NOT NULL,          -- the Kafka message envelope: {job_id, job_type}
    published_at TIMESTAMPTZ                    -- NULL until the dispatcher sends it
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Per-type config: the base payload (master set of keys with defaults; the
-- worker overlays each run's input on it at claim) plus stuck-run tuning.
-- run_timeout/max_attempts fall back to defaults when absent; a type with no
-- row is un-runnable (the worker fails the run gracefully).
CREATE TABLE job_type_config (
    job_type      TEXT PRIMARY KEY,
    payload       JSONB    NOT NULL DEFAULT '{}'::jsonb,
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',
    max_attempts  INT      NOT NULL DEFAULT 3
);
```

### 9.2 Handler — enqueue (one transaction)

```sql
BEGIN;
  -- dedup is enforced by the partial unique index; 0 rows back = already active, skip.
  -- input_payload is the producer's optional overrides ('{}' when none); the
  -- worker merges it onto the base config at claim. No business data on the wire.
  INSERT INTO jobs (job_type, input_payload)
  VALUES (:job_type, :input_payload)
  ON CONFLICT DO NOTHING
  RETURNING id;

  -- only if a row was inserted (use the RETURNING id):
  -- outbox payload is the pointer envelope; no business data on the wire.
  INSERT INTO outbox (job_id, payload)
  VALUES (:job_id, jsonb_build_object(
            'job_id', :job_id, 'job_type', :job_type));
COMMIT;
```

### 9.3 Dispatcher — drain outbox → Kafka (loop; safe with N replicas)

```sql
-- 1) claim a batch of unpublished messages
SELECT id, job_id, payload
FROM outbox
WHERE published_at IS NULL
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 100;

-- 2) publish each to Kafka (key = job_id), then per row, in one tx:
BEGIN;
  UPDATE outbox SET published_at = now() WHERE id = :outbox_id;
  UPDATE jobs   SET status = 'dispatched', updated_at = now()
    WHERE id = :job_id AND status = 'queued';
COMMIT;
```

### 9.4 Worker — claim, process, report

```sql
-- claim (compare-and-set): exactly one worker wins a redelivered message
-- accept queued OR dispatched: the dispatcher publishes to Kafka before it marks
-- the row 'dispatched', so a fast worker can outrace that and see it still 'queued'
UPDATE jobs SET status = 'running', updated_at = now()
WHERE id = :job_id AND status IN ('queued', 'dispatched');
-- 0 rows affected => someone else owns it (or it's terminal) => ack & skip

-- resolve the effective payload: base config overlaid with this run's input
-- (|| is a shallow merge, right side wins), snapshotted so the run records what
-- it used. 0 rows => no job_type_config row for this type => fail gracefully.
UPDATE jobs j
SET payload = c.payload || j.input_payload, updated_at = now()
FROM job_type_config c
WHERE j.id = :job_id AND c.job_type = j.job_type AND j.status = 'running'
RETURNING j.payload;

-- on success (then commit Kafka offset AFTER this commits):
UPDATE jobs SET status = 'completed', updated_at = now()
WHERE id = :job_id AND status = 'running';

-- on failure:
UPDATE jobs SET status = 'failed', updated_at = now()
WHERE id = :job_id AND status = 'running';
```

### 9.5 Reaper — stuck-`running` recovery (1 replica; ~1 min timer)

```sql
-- 1) find stuck runs, per-type timeout
SELECT j.id, j.attempts, c.max_attempts
FROM jobs j
JOIN job_type_config c USING (job_type)
WHERE j.status = 'running'
  AND j.updated_at < now() - c.run_timeout;
-- (types without a config row: COALESCE to defaults, or LEFT JOIN + defaults)

-- 2a) under the cap: re-queue + re-arm the outbox (one tx)
BEGIN;
  UPDATE jobs SET status = 'queued', attempts = attempts + 1, updated_at = now()
    WHERE id = :id AND status = 'running' AND attempts < :max_attempts;
  UPDATE outbox SET published_at = NULL WHERE job_id = :id;   -- or INSERT a fresh row
COMMIT;

-- 2b) cap reached: give up -> terminal, frees the dedup slot
UPDATE jobs SET status = 'failed', updated_at = now()
  WHERE id = :id AND status = 'running' AND attempts >= :max_attempts;
```

### 9.6 Operational / history reads

```sql
-- every run of a type this month and how it ended
SELECT id, status, created_at, updated_at
FROM jobs
WHERE job_type = :job_type AND created_at >= date_trunc('month', now())
ORDER BY created_at DESC;

-- what's in flight right now
SELECT job_type, status, count(*)
FROM jobs
WHERE status IN ('queued', 'dispatched', 'running')
GROUP BY job_type, status;

-- runs stuck longer than their timeout (alerting)
SELECT j.id, j.job_type, j.updated_at
FROM jobs j JOIN job_type_config c USING (job_type)
WHERE j.status = 'running' AND j.updated_at < now() - c.run_timeout;
```

---

## 10. HTTP API (`handlerAPI/`)

The `handler/` CLI stays as-is (it's still the K8s-Job/shell producer contract).
`handlerAPI/` adds a **long-running FastAPI service** that exposes the same
enqueue over HTTP so AutoSys, other machines, and humans can call it. It's the
first slice of what will grow into a BFF (login/logout, server-side PKCE, cookie
sessions) — hence versioned routes and a resource-server core that a cookie
session can later plug into without changing the endpoints.

**Key invariant preserved:** the API calls `store.enqueue(job_type, input, creator)`
— the *same* jobs+outbox+dedup transaction the CLI uses. It never publishes to
Kafka and never writes its own `INSERT`. The only new data is *who* enqueued
(migration 004: `created_by_sub/type/client`), taken from the validated token.

### 10.1 Endpoints

```
POST /v1/jobs         # scope jobs.write; body {job_type, payload?}; 201 + Location, or 409 (active dup)
GET  /v1/jobs/{id}    # scope jobs.read; ownership-aware; 404 when absent/not-visible
GET  /healthz         # liveness
GET  /readyz          # readiness (checks the datastore)
```

### 10.2 Auth — OIDC resource server (Ping Federate)

Every protected call carries `Authorization: Bearer <JWT>`. Validation is local
(no per-request IdP round-trip):

1. select the signing key by `kid` from the issuer's **JWKS** (cached, TTL +
   refresh-on-unknown-`kid` to survive key rotation);
2. verify signature (RS256 by default, configurable) + `iss` / `aud` / `exp` /
   `nbf` with a clock-skew leeway;
3. distil claims into a `Principal` (subject, `user`|`service` type, `client_id`,
   scopes, groups).

Both caller styles produce a token the API validates identically:

- **M2M** (AutoSys): `client_credentials` (client id + secret). See
  `scripts/enqueue_job.sh`.
- **Human / web app**: authorization-code + **PKCE** (the SPA does the exchange
  for now; moving it server-side is the BFF phase).

`user` vs `service` is inferred from claims (a user-identity claim like `email`,
or `sub != client_id`). **Scopes are provisional and configurable**
(`jobs.write` / `jobs.read` / `jobs.read.all`); Ping is expected to also return
**AD groups**, which are already captured on the `Principal` so authorization can
key off them once the claim shape is confirmed — no endpoint changes needed.

### 10.3 Read ownership

`GET /v1/jobs/{id}` is ownership-aware: a human user reads only jobs they
created; a `jobs.read.all` holder or a service principal reads any. Jobs the
caller may not see return **404** (existence hidden), not 403. The rule lives in
one function (`deps.can_read_job_created_by`) so a future group-based policy is a
one-line change.

### 10.4 Production hardening

- **RFC 7807** `application/problem+json` for every error; internals (SQL, stack
  traces) never leak — a request-id header ties a 500 back to the logs.
- Strict input validation (unknown fields rejected; `job_type` pattern-checked).
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`, CSP, HSTS) +
  per-request id; CORS allowlist.
- **Rate limiting in-app** (sliding window via the `limits` library, as pure
  ASGI middleware — no coupling to framework router internals), keyed per bearer
  credential, `RATE_LIMIT` configurable; 429 as problem+json.
- **TLS in-app** via `TLS_CERTFILE` / `TLS_KEYFILE` (plain HTTP when unset).
- Runs as a non-root user in a dedicated image (`handlerAPI/Dockerfile`),
  separate from the worker image so uvicorn/FastAPI aren't pulled into the other
  components.

> **Interim vs. target.** Rate limiting *and* TLS run in-app **now** by request,
> but are expected to move to an **API gateway / load balancer** later; when they
> do, run uvicorn behind the proxy with `--proxy-headers` and keep the in-app
> limiter as defence in depth. The gateway would also host coarse quotas while
> the app keeps per-principal fairness.

### 10.5 Local stack

`docker compose up handlerapi` also starts `mock-oidc`
(`ghcr.io/navikt/mock-oauth2-server`, config in `deploy/mock-oidc.json`) as a
stand-in for Ping — it issues tokens carrying the `aud` + `scope` (and, for the
auth-code demo, `email` + `groups`) claims the API expects. This is **not** for
production; point `OIDC_ISSUER` / `OIDC_JWKS_URL` at Ping there. CI's e2e job
exercises the full `token → 401 (no token) → 201 → 200 → 409` path against this
stack.

---

## Summary

Four components (handler, dispatcher, worker, reaper) in separate repos + a shared contract lib, and just three tables: **`jobs`** (current state *and* durable per-run history, since rows accumulate one per run), a tiny **`outbox`**, and a small **`job_type_config`** (which now also holds each type's business **payload**). Producers only name the `job_type`; the Kafka envelope is a pure pointer (`{job_id, job_type}`) and the worker snapshots the config payload into `jobs` at claim time (missing config → the run fails gracefully). Two ideas do the heavy lifting: (1) the **partial unique index** that turns your queued-or-running check into something the database enforces atomically, and (2) the **outbox** so the handler writes the job and its Kafka message in one transaction and the dispatcher sends it reliably. Dedup, the worker claim guard, run history, and the SIEM correlation id all live on `jobs`. `job_type` is data everywhere except the worker, so **a new job type is a worker-only deploy**. Everything else is plain status transitions.
