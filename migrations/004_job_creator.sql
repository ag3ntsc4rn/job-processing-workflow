-- Records who enqueued each job, populated by the HTTP API from the validated
-- OIDC token. Nullable because producers that predate the API (or the CLI) may
-- not supply an identity. `job_id` remains the SIEM correlation id; these
-- columns let operators trace a run back to the user or service that created
-- it. Idempotent.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS created_by_sub    TEXT,  -- token `sub` (stable user/service id)
    ADD COLUMN IF NOT EXISTS created_by_type   TEXT,  -- 'user' | 'service'
    ADD COLUMN IF NOT EXISTS created_by_client TEXT;  -- calling app's client_id/azp

CREATE INDEX IF NOT EXISTS jobs_created_by_sub_idx ON jobs (created_by_sub);
