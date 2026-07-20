"""HTTP tests: identity, authz (scopes), validation, dedup, hardening.

Authentication (signature/issuer/audience/expiry) is owned by the upstream
enterprise JWT auth middleware and is out of scope here; these tests exercise
what this service actually does — read the validated claims and enforce scopes.
v2 is machine-to-machine only: every caller is a service and reads are not
ownership-gated, so any holder of ``jobs.read`` can read any job.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import Settings
from main import create_app
from store.memory import InMemoryStore
from tests.conftest import auth, make_token


def _svc_token(*, scope: str = "jobs.read jobs.write", sub: str = "svc") -> str:
    return make_token(scope=scope, sub=sub, client_id="svc-app")


@pytest.fixture
def settings() -> Settings:
    # dev mode: no signature verification, claims read from the token payload.
    return Settings(database_url=None, auth_verify=False)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(settings: Settings, store: InMemoryStore) -> TestClient:
    app = create_app(settings=settings, store=store)
    return TestClient(app)


# --- identity (claims present) ----------------------------------------------

def test_post_requires_authenticated_identity(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_post_rejects_malformed_token(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth("not-a-jwt"))
    assert resp.status_code == 401


# --- authorization (authz / scopes) -----------------------------------------

def test_post_requires_write_scope(client: TestClient):
    token = _svc_token(scope="jobs.read")  # read-only client
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth(token))
    assert resp.status_code == 403


def test_get_requires_read_scope(client: TestClient):
    write = _svc_token(scope="jobs.write")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth(write))
    job_id = created.json()["job_id"]
    resp = client.get(f"/v1/jobs/{job_id}", headers=auth(write))
    assert resp.status_code == 403


# --- create + dedup ---------------------------------------------------------

def test_create_job_success_records_service_creator(client: TestClient):
    token = _svc_token()
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "payload": {"x": 1}}, headers=auth(token)
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"
    assert resp.headers["Location"] == f"/v1/jobs/{job_id}"

    got = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert got["input_payload"] == {"x": 1}
    assert got["created_by"] == {"sub": "svc", "type": "service", "client_id": "svc-app"}


def test_create_job_dedup_conflict(client: TestClient):
    token = _svc_token()
    first = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth(token))
    assert first.status_code == 201
    second = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth(token))
    assert second.status_code == 409


# --- validation -------------------------------------------------------------

def test_create_job_rejects_bad_job_type(client: TestClient):
    resp = client.post(
        "/v1/jobs", json={"job_type": "Bad Type!"}, headers=auth(_svc_token())
    )
    assert resp.status_code == 422


def test_create_job_rejects_unknown_fields(client: TestClient):
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "bogus": 1}, headers=auth(_svc_token())
    )
    assert resp.status_code == 422


# --- reads (no ownership in v2) ---------------------------------------------

def test_any_service_can_read_any_job(client: TestClient):
    creator = _svc_token(sub="svc-a")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=auth(creator))
    job_id = created.json()["job_id"]

    other = _svc_token(sub="svc-b")  # different principal, still 200 in v2
    got = client.get(f"/v1/jobs/{job_id}", headers=auth(other))
    assert got.status_code == 200
    assert got.json()["created_by"]["type"] == "service"


def test_get_missing_job_returns_404(client: TestClient):
    resp = client.get("/v1/jobs/999999", headers=auth(_svc_token()))
    assert resp.status_code == 404


# --- ops + hardening --------------------------------------------------------

def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz(client: TestClient):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_security_headers_present(client: TestClient):
    resp = client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in resp.headers
    assert "strict-transport-security" in resp.headers
    assert resp.headers.get("x-request-id")


def test_unknown_path_returns_problem_404(client: TestClient):
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_readyz_reports_unavailable_when_store_down(settings):
    class _BrokenStore(InMemoryStore):
        def get_job(self, job_id: int):
            raise RuntimeError("db down")

    app = create_app(settings=settings, store=_BrokenStore())
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503


def test_internal_error_is_masked(settings):
    class _ExplodingStore(InMemoryStore):
        def enqueue(self, *a, **k):
            raise RuntimeError("boom with secret db url")

    app = create_app(settings=settings, store=_ExplodingStore())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello"}, headers=auth(_svc_token())
    )
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert resp.json()["title"] == "Internal server error"


# --- store selection --------------------------------------------------------

def test_defaults_to_in_memory_store_when_no_database_url(settings):
    # No injected store + database_url is None -> lifespan builds InMemoryStore.
    app = create_app(settings=settings)
    with TestClient(app) as client:  # entering the context runs the lifespan
        assert client.get("/healthz").status_code == 200
        assert isinstance(app.state.store, InMemoryStore)
