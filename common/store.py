"""The ``Store`` abstraction shared by all components.

Every DB interaction the components need is a method here, expressed at the
level of the *operation* (enqueue, claim, requeue-stuck) rather than raw SQL.
That keeps the component logic pure and lets the unit tests run against
``InMemoryStore`` with no Postgres. ``PostgresStore`` (in ``common.db``) is the
production implementation and mirrors these semantics exactly.
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Protocol

from common.models import Job, JobStatus, OutboxMessage, StuckJob, build_envelope, utcnow


class Store(Protocol):
    # --- handler ---
    def enqueue(self, job_type: str, payload: dict) -> int | None:
        """Insert a job + its outbox row in one transaction.

        Returns the new job id, or ``None`` if an active job of this type
        already exists (dedup) so the caller should skip.
        """
        ...

    # --- dispatcher ---
    def fetch_unpublished(self, limit: int) -> list[OutboxMessage]: ...
    def mark_dispatched(self, outbox_id: int, job_id: int) -> None: ...

    # --- worker ---
    def claim(self, job_id: int) -> bool:
        """Compare-and-set ``dispatched -> running``. True iff this caller won."""
        ...

    def complete(self, job_id: int) -> bool: ...
    def fail(self, job_id: int) -> bool: ...

    # --- reaper ---
    def find_stuck(self) -> list[StuckJob]: ...
    def requeue_stuck(self, job_id: int) -> bool:
        """Re-queue a stuck run + re-arm its outbox, if under the type's cap.

        True iff re-queued; False means the churn cap is reached and the caller
        should dead-letter it.
        """
        ...

    def dead_letter(self, job_id: int) -> bool: ...

    # --- shared / read ---
    def get_job(self, job_id: int) -> Job | None: ...


DEFAULT_RUN_TIMEOUT = timedelta(minutes=15)
DEFAULT_MAX_ATTEMPTS = 3


class InMemoryStore:
    """Process-local ``Store`` used by the unit tests and for service-less dev.

    Faithfully mirrors the Postgres semantics that matter for correctness:
    dedup on active status, the compare-and-set claim, and the reaper's
    guarded re-queue/dead-letter. Timeouts are configurable per type via
    ``set_type_config``; unset types fall back to the defaults.
    """

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._outbox: dict[int, OutboxMessage] = {}
        self._published: set[int] = set()
        self._type_config: dict[str, tuple[timedelta, int]] = {}
        self._job_ids = itertools.count(1)
        self._outbox_ids = itertools.count(1)

    # -- test helpers ------------------------------------------------------
    def set_type_config(self, job_type: str, run_timeout: timedelta, max_attempts: int) -> None:
        self._type_config[job_type] = (run_timeout, max_attempts)

    def _timeout(self, job_type: str) -> timedelta:
        return self._type_config.get(job_type, (DEFAULT_RUN_TIMEOUT, DEFAULT_MAX_ATTEMPTS))[0]

    def _max_attempts(self, job_type: str) -> int:
        return self._type_config.get(job_type, (DEFAULT_RUN_TIMEOUT, DEFAULT_MAX_ATTEMPTS))[1]

    def _active(self, job_type: str) -> bool:
        return any(
            j.job_type == job_type and j.status in JobStatus.ACTIVE
            for j in self._jobs.values()
        )

    # -- handler -----------------------------------------------------------
    def enqueue(self, job_type: str, payload: dict) -> int | None:
        if self._active(job_type):
            return None
        job_id = next(self._job_ids)
        self._jobs[job_id] = Job(
            id=job_id, job_type=job_type, status=JobStatus.QUEUED, payload=payload
        )
        outbox_id = next(self._outbox_ids)
        self._outbox[outbox_id] = OutboxMessage(
            id=outbox_id, job_id=job_id, payload=build_envelope(job_id, job_type, payload)
        )
        return job_id

    # -- dispatcher --------------------------------------------------------
    def fetch_unpublished(self, limit: int) -> list[OutboxMessage]:
        rows = [m for mid, m in sorted(self._outbox.items()) if mid not in self._published]
        return rows[:limit]

    def mark_dispatched(self, outbox_id: int, job_id: int) -> None:
        self._published.add(outbox_id)
        job = self._jobs.get(job_id)
        if job and job.status == JobStatus.QUEUED:
            job.status = JobStatus.DISPATCHED
            job.updated_at = utcnow()

    # -- worker ------------------------------------------------------------
    def claim(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        # Accept queued OR dispatched: receiving the message already proves it
        # was published, and a fast worker can outrace the dispatcher's
        # publish -> mark-dispatched step, seeing the row still 'queued'.
        if job and job.status in (JobStatus.QUEUED, JobStatus.DISPATCHED):
            job.status = JobStatus.RUNNING
            job.updated_at = utcnow()
            return True
        return False

    def complete(self, job_id: int) -> bool:
        return self._terminal(job_id, JobStatus.COMPLETED)

    def fail(self, job_id: int) -> bool:
        return self._terminal(job_id, JobStatus.FAILED)

    def _terminal(self, job_id: int, status: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status == JobStatus.RUNNING:
            job.status = status
            job.updated_at = utcnow()
            return True
        return False

    # -- reaper ------------------------------------------------------------
    def find_stuck(self) -> list[StuckJob]:
        now = utcnow()
        stuck = []
        for job in self._jobs.values():
            if job.status == JobStatus.RUNNING and now - job.updated_at > self._timeout(
                job.job_type
            ):
                stuck.append(StuckJob(job_id=job.id, job_type=job.job_type, attempts=job.attempts))
        return stuck

    def requeue_stuck(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.RUNNING:
            return False
        if job.attempts >= self._max_attempts(job.job_type):
            return False
        job.status = JobStatus.QUEUED
        job.attempts += 1
        job.updated_at = utcnow()
        # re-arm: make the job's outbox message publishable again
        for mid, msg in self._outbox.items():
            if msg.job_id == job_id:
                self._published.discard(mid)
        return True

    def dead_letter(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status == JobStatus.RUNNING:
            job.status = JobStatus.FAILED
            job.updated_at = utcnow()
            return True
        return False

    # -- read --------------------------------------------------------------
    def get_job(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)
