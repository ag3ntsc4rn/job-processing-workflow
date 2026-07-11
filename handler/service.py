"""Enqueue logic — pure, so it unit-tests against any ``Store``."""

from __future__ import annotations

from dataclasses import dataclass

from common.store import Store


@dataclass
class EnqueueResult:
    job_id: int | None
    enqueued: bool


def enqueue(store: Store, job_type: str, payload: dict | None = None) -> EnqueueResult:
    """Enqueue a job unless an active one of the same type already exists.

    The producer names the ``job_type`` and may optionally pass a ``payload`` of
    key overrides. The type's base config (``job_type_config.payload``, the
    master set of keys with defaults) is authoritative; the worker overlays this
    input on top of it at claim time (input wins per key). The dedup decision is
    made atomically by the store (partial unique index), not by a read-then-write
    here, so concurrent handlers are safe.

    This function is the single enqueue contract: any producer (the CLI, an HTTP
    endpoint, another Python service) should go through it rather than writing
    its own ``INSERT INTO jobs`` so the jobs+outbox+dedup invariant lives in one
    place.
    """
    job_id = store.enqueue(job_type, payload or {})
    return EnqueueResult(job_id=job_id, enqueued=job_id is not None)
