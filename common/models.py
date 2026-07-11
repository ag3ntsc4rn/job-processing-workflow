"""Job domain model, statuses, and the Kafka message envelope.

These definitions are the contract shared by every component, so they live in
``common`` and nothing here imports a component. ``job_type`` is deliberately
just a string carried through the whole pipeline — only the worker maps it to
behaviour, which is why adding a new job type is a worker-only change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class JobStatus:
    """The lifecycle states a job moves through.

    ``ACTIVE`` are the non-terminal states; the partial unique index that
    enforces "one active job per type" keys off exactly this set.
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
class Job:
    id: int
    job_type: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class OutboxMessage:
    """A row from the outbox: the exact JSON to publish for one job."""

    id: int
    job_id: int
    payload: dict[str, Any]


@dataclass
class StuckJob:
    """A run the reaper found sitting in ``running`` past its type's timeout."""

    job_id: int
    job_type: str
    attempts: int


def build_envelope(job_id: int, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The single message envelope every job type shares on the Kafka topic."""
    return {"job_id": job_id, "job_type": job_type, "payload": payload}
