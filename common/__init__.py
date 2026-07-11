"""Shared contract for the job-orchestrator components.

Everything that must stay identical across the handler, dispatcher, worker and
reaper lives here: the job model and statuses, the Kafka message envelope, the
runtime config, and the ``Store`` abstraction (with a Postgres implementation
and an in-memory double used by the tests).
"""
