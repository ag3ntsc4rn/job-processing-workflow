-- Job orchestrator schema. Idempotent: safe to run on every startup.

-- Current state + durable per-run history (rows are never overwritten).
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued',  -- queued|dispatched|running|completed|failed
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    attempts    INT         NOT NULL DEFAULT 0,         -- reaper re-dispatch count (churn cap)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one ACTIVE job per job_type. "Active" = every non-terminal status.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_type
    ON jobs (job_type)
    WHERE status IN ('queued', 'dispatched', 'running');

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_type_created_idx ON jobs (job_type, created_at DESC);

-- Transactional outbox: written in the same tx as the job row; drained to Kafka.
CREATE TABLE IF NOT EXISTS outbox (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id),
    payload      JSONB       NOT NULL,                  -- envelope: {job_id, job_type}
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Per-type tuning (timeouts / churn cap). Missing rows fall back to defaults.
CREATE TABLE IF NOT EXISTS job_type_config (
    job_type      TEXT PRIMARY KEY,
    run_timeout   INTERVAL NOT NULL DEFAULT '15 min',
    max_attempts  INT      NOT NULL DEFAULT 3
);
