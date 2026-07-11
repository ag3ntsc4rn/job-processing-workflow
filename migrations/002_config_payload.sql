-- Move the business payload into job_type_config: producers (AutoSys/handler)
-- now only name the job_type; the worker snapshots this payload into jobs at
-- claim time. Idempotent: safe to run on every startup.

ALTER TABLE job_type_config
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Demo seed so the compose walkthrough has runnable types out of the box.
-- A type with no job_type_config row is intentionally un-runnable: the worker
-- fails such a run (there is no payload/config to run it with).
INSERT INTO job_type_config (job_type, payload) VALUES
    ('hello', '{"name": "Ada"}'::jsonb),
    ('boom',  '{}'::jsonb)
ON CONFLICT (job_type) DO NOTHING;
