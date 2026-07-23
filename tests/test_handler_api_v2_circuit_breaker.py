"""Tests for handlerAPIv2's datastore circuit breaker (pybreaker) and its decorator."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient

from common.models import Creator
from common.store import InMemoryStore, Store
from handlerAPIv2.app import create_app
from handlerAPIv2.auth import build_verifier
from handlerAPIv2.circuit_breaker import (
    CircuitBreakerStore,
    CircuitOpenError,
    build_breaker,
)
from handlerAPIv2.config import Settings
from tests.conftest import AUDIENCE, ISSUER, JWKS_URL, TokenFactory


class _FailingStore(InMemoryStore):
    """Every DB call blows up — used to drive the breaker open."""

    def enqueue(self, *_a, **_k) -> int | None:
        raise RuntimeError("db down")

    def get_job(self, *_a, **_k):
        raise RuntimeError("db down")


# --- store decorator --------------------------------------------------------

def test_store_decorator_delegates_when_closed():
    inner = InMemoryStore()
    inner.set_type_config("hello", payload={"name": "Ada"})
    store = CircuitBreakerStore(inner, build_breaker(failure_threshold=1))

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

@pytest.fixture
def v2_settings() -> Settings:
    return Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
        db_circuit_failure_threshold=1,
    )


def _client(v2_settings: Settings, tokens: TokenFactory) -> TestClient:
    store = CircuitBreakerStore(
        _FailingStore(),
        build_breaker(failure_threshold=v2_settings.db_circuit_failure_threshold),
    )
    verifier = build_verifier(v2_settings, http_get=tokens.http_get)
    # The wrapper guards only the API's two Store methods; cast for the strict param.
    app = create_app(settings=v2_settings, store=cast(Store, store), verifier=verifier)
    return TestClient(app, raise_server_exceptions=False)


def test_open_breaker_returns_503_on_write(v2_settings, tokens: TokenFactory):
    token = tokens.mint(scope="jobs.read jobs.write", client_id="svc-app")
    resp = _client(v2_settings, tokens).post(
        "/v1/jobs", json={"job_type": "hello"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_open_breaker_returns_503_on_readyz(v2_settings, tokens: TokenFactory):
    resp = _client(v2_settings, tokens).get("/readyz")
    assert resp.status_code == 503
