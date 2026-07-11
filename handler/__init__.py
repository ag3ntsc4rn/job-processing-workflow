"""Job handler: the enqueue step.

Receives a trigger (from AutoSys or any other producer) and inserts a job +
outbox row in one transaction, deduping on active status. Only touches
Postgres, so the caller gets a fast ack. ``job_type`` is passed straight
through — the handler has no per-type logic.
"""
