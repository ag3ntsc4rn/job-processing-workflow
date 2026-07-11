-- Producers may optionally send a JSON payload at enqueue that overrides the
-- type's base config. We keep that request payload separately (audit of what
-- was sent); jobs.payload is the *effective* config the worker snapshots at
-- claim (base overlaid with input, input wins). Idempotent.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS input_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
