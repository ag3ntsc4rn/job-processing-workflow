-- ============================================================================
-- Job pipeline schema — dispatcher + worker + reaper (standalone services).
--
-- Run once against your database (pgAdmin: open, then Execute). Idempotent, so
-- re-running is safe.
--
-- None of the three services create or migrate this schema; the producer owns
-- it. Each repo's deploy/schema.sql declares only the slice that service
-- touches, which is why no single one of them is complete — this is.
--
-- NOT a replacement for migrations/ in this repo. That schema is a superset:
-- it also carries the payload plumbing (jobs.payload, jobs.input_payload,
-- job_type_config.payload) and the API's created_by_* columns, which the
-- monolith's worker resolves at claim time. The standalone worker ships a
-- generic handler that resolves no payload, so nothing in the three services
-- reads those columns. Run migrations/ for this repo; run this file for a
-- deployment of job-dispatcher + job-worker + job-reaper.
--
-- Job lifecycle:
--   queued --dispatcher--> dispatched --worker--> running --> completed | failed
--                                            reaper: running --> queued (re-armed)
--                                                    running --> failed (churn cap)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- jobs: current state of every run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued',  -- queued|dispatched|running|completed|failed
    attempts    INT         NOT NULL DEFAULT 0,         -- reaper re-queue count (churn cap)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()      -- reaper's stuck-run clock
);

-- At most one ACTIVE run per job_type ("active" = every non-terminal status).
-- This is what makes a stranded 'running' row block the whole type, and why the
-- reaper dead-letters to 'failed' rather than leaving it: 'failed' is terminal,
-- so it releases the slot.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_type
    ON jobs (job_type)
    WHERE status IN ('queued', 'dispatched', 'running');

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);

-- The reaper's hot path: running runs, oldest heartbeat first.
CREATE INDEX IF NOT EXISTS jobs_running_updated_idx
    ON jobs (updated_at)
    WHERE status = 'running';

-- ---------------------------------------------------------------------------
-- outbox: transactional outbox, written in the same transaction as the job row.
-- The dispatcher drains it; the reaper re-arms a row by nulling published_at.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id),
    payload      JSONB       NOT NULL,   -- envelope, copied verbatim to Kafka: {job_id, job_type}
    published_at TIMESTAMPTZ             -- NULL == pending
);

-- The dispatcher's hot path: pending rows, oldest first.
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- The reaper's re-arm lookup.
CREATE INDEX IF NOT EXISTS outbox_job_id_idx ON outbox (job_id);

-- ---------------------------------------------------------------------------
-- job_type_config: per-type reaper tuning. Optional — a type with no row falls
-- back to the reaper's REAPER_DEFAULT_RUN_TIMEOUT / REAPER_DEFAULT_MAX_ATTEMPTS.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_type_config (
    job_type      TEXT PRIMARY KEY,
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',  -- 'running' longer than this == stuck
    max_attempts  INT      NOT NULL DEFAULT 3          -- reaper re-queues before dead-lettering
);

-- Example tuning; delete or edit as needed.
-- INSERT INTO job_type_config (job_type, run_timeout, max_attempts) VALUES
--     ('hello',      '5 min',  3),
--     ('nightly_etl', '4 hours', 1)
-- ON CONFLICT (job_type) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Enqueue one job, the way a producer must: row + outbox in ONE transaction.
-- That atomicity is the entire point of the outbox — uncomment to smoke-test.
-- ---------------------------------------------------------------------------
-- BEGIN;
-- WITH new_job AS (
--     INSERT INTO jobs (job_type) VALUES ('hello') RETURNING id, job_type
-- )
-- INSERT INTO outbox (job_id, payload)
-- SELECT id, jsonb_build_object('job_id', id, 'job_type', job_type) FROM new_job;
-- COMMIT;
