"""Job domain model, statuses, and the outbox message envelope.

These are the contract this service shares with the wider job-processing
platform (dispatcher/worker/reaper live in their own repos/services). ``job_type``
is deliberately just a string carried through the pipeline; only the worker maps
it to behaviour, so adding a new job type never touches this API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class JobStatus:
    """The lifecycle states a job moves through.

    ``ACTIVE`` are the non-terminal states; the "one active job per type" dedup
    (a partial unique index in Postgres) keys off exactly this set.
    """

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    ACTIVE = (QUEUED, DISPATCHED, RUNNING)
    TERMINAL = (COMPLETED, FAILED)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Creator:
    """Identity of whoever enqueued a job, taken from the validated JWT.

    v2 is machine-to-machine only, so ``type`` is always ``'service'`` and
    ``client_id`` is the calling application. All fields optional so non-API
    producers could still enqueue without an identity.
    """

    sub: str | None = None
    type: str | None = None
    client_id: str | None = None


@dataclass
class Job:
    id: int
    job_type: str
    status: str
    # what the producer sent at enqueue (optional overrides); audit of the request
    input_payload: dict[str, Any] = field(default_factory=dict)
    # effective config the run used: base config overlaid with input, snapshotted at claim
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    # who enqueued it (populated by the API from the token)
    created_by: Creator | None = None


@dataclass
class OutboxMessage:
    """A row from the outbox: the exact JSON to publish for one job."""

    id: int
    job_id: int
    payload: dict[str, Any]


def build_envelope(job_id: int, job_type: str) -> dict[str, Any]:
    """The pointer envelope published for a job.

    Deliberately a pure pointer: ``job_id`` identifies the run and ``job_type``
    routes it. The business payload is not carried here — the worker resolves it
    from config when it claims the run.
    """
    return {"job_id": job_id, "job_type": job_type}
