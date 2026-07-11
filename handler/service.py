"""Enqueue logic — pure, so it unit-tests against any ``Store``."""

from __future__ import annotations

from dataclasses import dataclass

from common.store import Store


@dataclass
class EnqueueResult:
    job_id: int | None
    enqueued: bool


def enqueue(store: Store, job_type: str) -> EnqueueResult:
    """Enqueue a job unless an active one of the same type already exists.

    The handler only names the ``job_type`` — no business data flows through it
    (that lives in ``job_type_config`` and is snapshotted by the worker). The
    dedup decision is made atomically by the store (partial unique index), not
    by a read-then-write here, so concurrent handlers are safe.
    """
    job_id = store.enqueue(job_type)
    return EnqueueResult(job_id=job_id, enqueued=job_id is not None)
