"""Tests for the datastore circuit breaker (pybreaker) and its ``Store`` decorator."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import Settings
from domain.models import Creator
from main import create_app
from store.circuit_breaker import CircuitBreakerStore, CircuitOpenError, build_breaker
from store.memory import InMemoryStore
from tests.conftest import auth, make_token


class _FailingStore(InMemoryStore):
    """Every DB call blows up — used to drive the breaker open."""

    def enqueue(self, *_a, **_k) -> int | None:
        raise RuntimeError("db down")

    def get_job(self, *_a, **_k):
        raise RuntimeError("db down")


# --- store decorator --------------------------------------------------------

def test_store_decorator_delegates_when_closed():
    store = CircuitBreakerStore(InMemoryStore(), build_breaker(failure_threshold=1))

    job_id = store.enqueue("hello", {}, Creator(sub="svc"))
    assert job_id == 1
    got = store.get_job(job_id)
    assert got is not None and got.job_type == "hello"
    assert store.state == "closed"


def test_store_decorator_trips_open_and_fails_fast():
    store = CircuitBreakerStore(_FailingStore(), build_breaker(failure_threshold=1))
    with pytest.raises(CircuitOpenError):
        store.enqueue("hello")  # first failure trips the breaker
    assert store.state == "open"
    # Once open the inner store is not touched — it rejects immediately.
    with pytest.raises(CircuitOpenError):
        store.get_job(1)


def test_store_decorator_close_is_optional():
    closed = {"n": 0}

    class _Closable(InMemoryStore):
        def close(self) -> None:
            closed["n"] += 1

    CircuitBreakerStore(_Closable(), build_breaker()).close()
    assert closed["n"] == 1
    # InMemoryStore has no close(); wrapper must not raise.
    CircuitBreakerStore(InMemoryStore(), build_breaker()).close()


# --- end-to-end: open breaker surfaces as 503 -------------------------------

def _client() -> TestClient:
    store = CircuitBreakerStore(_FailingStore(), build_breaker(failure_threshold=1))
    app = create_app(settings=Settings(database_url=None), store=store)
    return TestClient(app, raise_server_exceptions=False)


def test_open_breaker_returns_503_on_write():
    resp = _client().post(
        "/v1/jobs", json={"job_type": "hello"}, headers=auth(make_token())
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_open_breaker_returns_503_on_readyz():
    resp = _client().get("/readyz")
    assert resp.status_code == 503
