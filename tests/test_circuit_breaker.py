"""Tests for the datastore circuit breaker and its ``Store`` decorator."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import Settings
from domain.models import Creator
from main import create_app
from store.base import Store
from store.circuit_breaker import CircuitBreaker, CircuitBreakerStore, CircuitOpenError
from store.memory import InMemoryStore
from tests.conftest import auth, make_token


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _boom() -> int:
    raise RuntimeError("db down")


def _ok() -> int:
    return 1


# --- breaker state machine --------------------------------------------------

def test_closed_passes_calls_through():
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.call(_ok) == 1
    assert cb.state == "closed"


def test_trips_open_after_threshold_then_fails_fast():
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=3, reset_timeout=30, time_fn=clock)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(_boom)
    assert cb.state == "open"

    # While open the DB is not touched — the call is rejected immediately.
    with pytest.raises(CircuitOpenError):
        cb.call(_ok)


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(_boom)
    cb.call(_ok)  # success resets the counter
    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(_boom)
    assert cb.state == "closed"  # only 2 consecutive failures since the reset


def test_half_open_trial_success_closes():
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=30, time_fn=clock)
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    assert cb.state == "open"

    clock.t += 30  # cooldown elapsed -> next call is the half-open trial
    assert cb.call(_ok) == 1
    assert cb.state == "closed"


def test_half_open_trial_failure_reopens():
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=30, time_fn=clock)
    with pytest.raises(RuntimeError):
        cb.call(_boom)

    clock.t += 30  # trial allowed
    with pytest.raises(RuntimeError):
        cb.call(_boom)  # trial fails -> reopen
    assert cb.state == "open"

    # Reopened: still fails fast until the next cooldown.
    with pytest.raises(CircuitOpenError):
        cb.call(_ok)
    clock.t += 30
    assert cb.call(_ok) == 1


# --- store decorator --------------------------------------------------------

def test_store_decorator_delegates_and_trips():
    inner = InMemoryStore()
    cb = CircuitBreaker(failure_threshold=1)
    store = CircuitBreakerStore(inner, cb)

    job_id = store.enqueue("hello", {}, Creator(sub="svc"))
    assert job_id == 1
    got = store.get_job(job_id)
    assert got is not None and got.job_type == "hello"
    assert store.state == "closed"


def test_store_decorator_close_is_optional():
    closed = {"n": 0}

    class _Closable(InMemoryStore):
        def close(self) -> None:
            closed["n"] += 1

    CircuitBreakerStore(_Closable(), CircuitBreaker()).close()
    assert closed["n"] == 1
    # InMemoryStore has no close(); wrapper must not raise.
    CircuitBreakerStore(InMemoryStore(), CircuitBreaker()).close()


# --- end-to-end: open breaker surfaces as 503 -------------------------------

class _OpenStore:
    """Stands in for a wrapped store whose breaker is already open."""

    def enqueue(self, *_a, **_k) -> int | None:
        raise CircuitOpenError("open")

    def get_job(self, *_a, **_k):
        raise CircuitOpenError("open")


def _client(store: Store) -> TestClient:
    return TestClient(create_app(settings=Settings(database_url=None), store=store))


def test_open_breaker_returns_503_on_write():
    resp = _client(_OpenStore()).post(
        "/v1/jobs", json={"job_type": "hello"}, headers=auth(make_token())
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_open_breaker_returns_503_on_readyz():
    resp = _client(_OpenStore()).get("/readyz")
    assert resp.status_code == 503
