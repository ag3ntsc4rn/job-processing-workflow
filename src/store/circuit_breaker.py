"""A small circuit breaker and a ``Store`` decorator that applies it.

Retrying a downed dependency (see ``PostgresStore._connect_with_retry``) helps a
*blip* but hurts a *sustained* outage: every request piles up waiting on a dead
database, exhausting the connection pool and worker threads until the failure
cascades to callers. A circuit breaker fixes that by *failing fast*:

* **closed** — calls pass through; consecutive failures are counted.
* **open** — once failures hit ``failure_threshold`` the breaker trips and, for
  ``reset_timeout`` seconds, rejects calls immediately with
  :class:`CircuitOpenError` (no DB hit, no waiting).
* **half-open** — after the cooldown a single trial call is allowed through;
  success closes the breaker, failure re-opens it for another cooldown.

``CircuitBreakerStore`` wraps any :class:`~store.base.Store` so the API's DB
calls go through the breaker without the routes knowing. It is threading-safe
because FastAPI runs the sync route handlers in a thread pool.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from domain.models import Creator, Job
from store.base import Store

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when the breaker is open and a call is rejected without running."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._now = time_fn
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None  # None => closed
        self._trial_in_flight = False  # a half-open trial is running

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half_open" if self._trial_in_flight else "open"

    def call(self, fn: Callable[[], T]) -> T:
        self._before()
        try:
            result = fn()
        except Exception:
            self._on_failure()
            raise
        self._on_success()
        return result

    def _before(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return  # closed: allow
            elapsed = self._now() - self._opened_at >= self._reset_timeout
            if not elapsed or self._trial_in_flight:
                raise CircuitOpenError("circuit open: datastore calls are rejected")
            # cooldown elapsed and no trial running: this call is the trial.
            self._trial_in_flight = True

    def _on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def _on_failure(self) -> None:
        with self._lock:
            if self._trial_in_flight:
                # half-open trial failed: re-open for another cooldown.
                self._trial_in_flight = False
                self._opened_at = self._now()
                return
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._now()


class CircuitBreakerStore:
    """Wraps a ``Store``, routing its calls through a :class:`CircuitBreaker`."""

    def __init__(self, inner: Store, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    @property
    def state(self) -> str:
        return self._breaker.state

    def enqueue(
        self,
        job_type: str,
        input_payload: dict | None = None,
        creator: Creator | None = None,
    ) -> int | None:
        return self._breaker.call(
            lambda: self._inner.enqueue(job_type, input_payload, creator)
        )

    def get_job(self, job_id: int) -> Job | None:
        return self._breaker.call(lambda: self._inner.get_job(job_id))

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()
