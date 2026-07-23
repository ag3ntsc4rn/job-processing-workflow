"""A ``Store`` decorator that guards the datastore with a circuit breaker.

``PostgresStore`` already retries at connect time, which tolerates a brief blip.
It does **not** help a *sustained* outage: every request then piles up waiting on
a dead database, exhausting the connection pool and worker threads until the
failure cascades to callers. A circuit breaker fixes that by *failing fast* —
once failures cross a threshold it trips **open** and rejects calls immediately
(no DB hit, no waiting) until a cooldown elapses and a trial call probes
recovery.

The breaker itself is `pybreaker` (thread-safe, which matters because FastAPI
runs the sync route handlers in a thread pool). ``CircuitBreakerStore`` wraps the
store so the API's two DB calls (``enqueue`` / ``get_job``) go through the
breaker without the routes knowing; when it is open, ``pybreaker`` raises
:class:`CircuitOpenError`, which the error layer maps to a ``503``.
"""

from __future__ import annotations

import pybreaker

from domain.models import Creator, Job
from store.base import Store

# Single import point for the "breaker is open" error (used by the error layer
# and tests) so callers don't depend on pybreaker directly.
CircuitOpenError = pybreaker.CircuitBreakerError


def build_breaker(
    *, failure_threshold: int = 5, reset_timeout: float = 30.0
) -> pybreaker.CircuitBreaker:
    return pybreaker.CircuitBreaker(
        fail_max=failure_threshold,
        reset_timeout=reset_timeout,
        name="postgres-store",
    )


class CircuitBreakerStore:
    """Wraps a ``Store``, routing its calls through a ``pybreaker`` breaker."""

    def __init__(self, inner: Store, breaker: pybreaker.CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    @property
    def state(self) -> str:
        return self._breaker.current_state

    def enqueue(
        self,
        job_type: str,
        input_payload: dict | None = None,
        creator: Creator | None = None,
    ) -> int | None:
        return self._breaker.call(self._inner.enqueue, job_type, input_payload, creator)

    def get_job(self, job_id: int) -> Job | None:
        return self._breaker.call(self._inner.get_job, job_id)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()
