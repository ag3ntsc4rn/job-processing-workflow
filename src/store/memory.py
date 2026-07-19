"""Process-local ``Store`` for demos and unit tests.

Faithfully mirrors the Postgres dedup rule that matters to this API: at most one
*active* job per type. It carries no infra, so the service runs end-to-end with
no database; set ``DATABASE_URL`` and :class:`~store.postgres.PostgresStore`
takes over with the same semantics.
"""

from __future__ import annotations

import itertools
from copy import deepcopy

from domain.models import Creator, Job, JobStatus


class InMemoryStore:
    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._job_ids = itertools.count(1)

    def _has_active(self, job_type: str) -> bool:
        return any(
            j.job_type == job_type and j.status in JobStatus.ACTIVE
            for j in self._jobs.values()
        )

    def enqueue(
        self,
        job_type: str,
        input_payload: dict | None = None,
        creator: Creator | None = None,
    ) -> int | None:
        if self._has_active(job_type):
            return None  # dedup: an active job of this type already exists
        job_id = next(self._job_ids)
        self._jobs[job_id] = Job(
            id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            input_payload=deepcopy(input_payload or {}),
            created_by=creator,
        )
        return job_id

    def get_job(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)
