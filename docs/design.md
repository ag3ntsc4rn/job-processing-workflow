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
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
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

Per-`job_type` config lives in a tiny lookup table (one row per type) so timeouts aren't hard-coded:

```sql
CREATE TABLE job_type_config (
    job_type      TEXT PRIMARY KEY,
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',   -- how long a run may sit in 'running' before it's stuck
    max_attempts  INT      NOT NULL DEFAULT 3           -- reclaim cap before dead-letter (§7)
);
```

> `attempts` here is **not** business retries (that's still Option A). It only counts how many times the *reaper* has re-dispatched a stuck run, so a poison job can't loop forever.

Plus a tiny **outbox** table so the handler never has to write to the DB and Kafka in the same breath (§3):

```sql
CREATE TABLE outbox (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id),
    payload      JSONB       NOT NULL,          -- the Kafka message
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
INSERT INTO jobs (job_type, payload) VALUES (?, ?)
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
  INSERT INTO jobs (job_type, payload) VALUES (?, ?)
    ON CONFLICT DO NOTHING            -- 0 rows = already active, skip
    RETURNING id;
  -- only if a row was inserted:
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

**Worker** — consumes from Kafka, does the work, sets `completed` or `failed`.

---

## 4. No separate executions table — `jobs` *is* the history

**Decision:** dropped `job_executions` entirely. The `jobs` table already gives us everything it would have, because rows are never overwritten across runs:

- **Every run is its own row.** The partial unique index (§1) only blocks a *second active* row per `job_type` — it does **not** delete or reuse the old one. So each successful queue creates a new row, and completed/failed rows stay in the table forever. Over time `jobs` naturally accumulates one row per run per `job_type` — that *is* the run history.
- **Timings come from the row itself.** `created_at` (queued), `updated_at` (last transition), and `status` tell you what happened to each run and roughly when. "Show me every `settlement` run this month and how each ended" is a single `SELECT ... WHERE job_type='settlement' ORDER BY created_at`.
- **`jobs.id` is the run/correlation id.** It's auto-generated and unique per row = unique per run, so the worker just stamps `jobs.id` on every log line it ships to the SIEM. From any DB row you pivot straight to that run's full step-by-step timeline in the SIEM. No second table needed to link them.
- **Claim guard lives on `jobs` too.** The worker claims a redelivered-safe run with a compare-and-set: `UPDATE jobs SET status='running' WHERE id=? AND status='dispatched'`. Only one worker's UPDATE matches; a duplicate delivery gets `0 rows` and simply acks and skips (§6). No `UNIQUE(job_id)` on a separate table required.

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

- **handler** — `INSERT INTO jobs(job_type, payload)`; `job_type` is passed straight through, no `switch`.
- **dispatcher** — copies `outbox` bytes to Kafka; never inspects the payload.
- **reaper** — acts on `status`/timeout; per-type timeouts come from the `job_type_config` **table**, not code.
- **worker** — the *only* place type logic lives. A registry (like a `@register("my_type")` decorator) maps `job_type` → a handler class. New type = add one handler, register it, deploy the worker.

The one non-code step is a `job_type_config` **row** (`run_timeout`, `max_attempts`) — a data insert, not a redeploy. Have the handler/reaper fall back to defaults when no row exists, so a new type runs on defaults and you only add a row to override them.

**Contract to hold the line:** every type shares one **message envelope** (`{job_id, job_type, payload}`) on one **topic**. As long as that's stable, the generic components never redeploy. A type needing a different envelope or its own topic/ordering is the only thing that would touch the dispatcher/contract — so agree on a stable envelope up front.

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
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    attempts    INT         NOT NULL DEFAULT 0,          -- reaper re-dispatch count (churn cap), NOT business retries
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    payload      JSONB       NOT NULL,          -- the Kafka message: {job_id, job_type, payload}
    published_at TIMESTAMPTZ                    -- NULL until the dispatcher sends it
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Per-type tuning (timeouts / churn cap). Optional per row: fall back to defaults.
CREATE TABLE job_type_config (
    job_type      TEXT PRIMARY KEY,
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',
    max_attempts  INT      NOT NULL DEFAULT 3
);
```

### 9.2 Handler — enqueue (one transaction)

```sql
BEGIN;
  -- dedup is enforced by the partial unique index; 0 rows back = already active, skip.
  INSERT INTO jobs (job_type, payload)
  VALUES (:job_type, :payload)
  ON CONFLICT DO NOTHING
  RETURNING id;

  -- only if a row was inserted (use the RETURNING id):
  INSERT INTO outbox (job_id, payload)
  VALUES (:job_id, jsonb_build_object(
            'job_id', :job_id, 'job_type', :job_type, 'payload', :payload));
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
UPDATE jobs SET status = 'running', updated_at = now()
WHERE id = :job_id AND status = 'dispatched';
-- 0 rows affected => someone else owns it (or it's not dispatchable) => ack & skip

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

## Summary

Four components (handler, dispatcher, worker, reaper) in separate repos + a shared contract lib, and just three tables: **`jobs`** (current state *and* durable per-run history, since rows accumulate one per run), a tiny **`outbox`**, and a small **`job_type_config`**. Two ideas do the heavy lifting: (1) the **partial unique index** that turns your queued-or-running check into something the database enforces atomically, and (2) the **outbox** so the handler writes the job and its Kafka message in one transaction and the dispatcher sends it reliably. Dedup, the worker claim guard, run history, and the SIEM correlation id all live on `jobs`. `job_type` is data everywhere except the worker, so **a new job type is a worker-only deploy**. Everything else is plain status transitions.
