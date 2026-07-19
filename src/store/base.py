"""The ``Store`` abstraction this API depends on.

The API needs exactly two operations — enqueue a job and read one back — so the
protocol is deliberately that small. ``InMemoryStore`` (demo / tests) and
``PostgresStore`` (production) both implement it with identical semantics, which
is what lets the app switch between them purely on whether ``DATABASE_URL`` is
set, with no change to route code.
"""

from __future__ import annotations

from typing import Protocol

from domain.models import Creator, Job


class Store(Protocol):
    def enqueue(
        self,
        job_type: str,
        input_payload: dict | None = None,
        creator: Creator | None = None,
    ) -> int | None:
        """Insert a job (and its outbox row) in one transaction.

        ``input_payload`` is the producer's optional per-run overrides; ``creator``
        is the enqueuing identity from the token. Returns the new job id, or
        ``None`` if an active job of this type already exists (dedup) so the caller
        can surface a conflict.
        """
        ...

    def get_job(self, job_id: int) -> Job | None:
        """Return the job, or ``None`` if it does not exist."""
        ...
