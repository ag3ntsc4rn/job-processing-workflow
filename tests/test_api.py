"""HTTP tests: authn, authz (scopes), validation, dedup, hardening.

v2 is machine-to-machine only: every caller is a service and reads are not
ownership-gated, so any holder of ``jobs.read`` can read any job. Tokens are
minted locally (see ``tests/conftest.py``) so the full JWT path runs with no
live IdP.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from auth.verifier import build_verifier
from config import Settings
from store.memory import InMemoryStore
from tests.conftest import AUDIENCE, ISSUER, JWKS_URL, TokenFactory


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _svc_token(
    tokens: TokenFactory, *, scope: str = "jobs.read jobs.write", sub: str = "svc"
) -> str:
    return tokens.mint(scope=scope, sub=sub, client_id="svc-app")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(settings: Settings, store: InMemoryStore, tokens: TokenFactory) -> TestClient:
    verifier = build_verifier(settings, http_get=tokens.http_get)
    app = create_app(settings=settings, store=store, verifier=verifier)
    return TestClient(app)


# --- authentication (authn) -------------------------------------------------

def test_post_requires_bearer(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_post_rejects_malformed_token(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth("not-a-jwt"))
    assert resp.status_code == 401


def test_post_rejects_expired_token(client: TestClient, tokens: TokenFactory):
    token = tokens.mint(exp_delta=-3600)
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_wrong_audience(client: TestClient, tokens: TokenFactory):
    token = tokens.mint(audience="some-other-api")
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_wrong_issuer(client: TestClient, tokens: TokenFactory):
    token = tokens.mint(issuer="https://evil.example.test")
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_unknown_kid(client: TestClient, tokens: TokenFactory):
    token = tokens.mint(kid="no-such-key")
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


# --- authorization (authz / scopes) -----------------------------------------

def test_post_requires_write_scope(client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens, scope="jobs.read")  # read-only client
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 403


def test_get_requires_read_scope(client: TestClient, tokens: TokenFactory):
    write = _svc_token(tokens, scope="jobs.write")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(write))
    job_id = created.json()["job_id"]
    resp = client.get(f"/v1/jobs/{job_id}", headers=_auth(write))
    assert resp.status_code == 403


# --- create + dedup ---------------------------------------------------------

def test_create_job_success_records_service_creator(client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "payload": {"x": 1}}, headers=_auth(token)
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"
    assert resp.headers["Location"] == f"/v1/jobs/{job_id}"

    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(token)).json()
    assert got["input_payload"] == {"x": 1}
    assert got["created_by"] == {"sub": "svc", "type": "service", "client_id": "svc-app"}


def test_create_job_dedup_conflict(client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens)
    first = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert first.status_code == 201
    second = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert second.status_code == 409


# --- validation -------------------------------------------------------------

def test_create_job_rejects_bad_job_type(client: TestClient, tokens: TokenFactory):
    resp = client.post(
        "/v1/jobs", json={"job_type": "Bad Type!"}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 422


def test_create_job_rejects_unknown_fields(client: TestClient, tokens: TokenFactory):
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "bogus": 1}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 422


# --- reads (no ownership in v2) ---------------------------------------------

def test_any_service_can_read_any_job(client: TestClient, tokens: TokenFactory):
    creator = _svc_token(tokens, sub="svc-a")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(creator))
    job_id = created.json()["job_id"]

    other = _svc_token(tokens, sub="svc-b")  # different principal, still 200 in v2
    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(other))
    assert got.status_code == 200
    assert got.json()["created_by"]["type"] == "service"


def test_get_missing_job_returns_404(client: TestClient, tokens: TokenFactory):
    resp = client.get("/v1/jobs/999999", headers=_auth(_svc_token(tokens)))
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


def test_readyz_reports_unavailable_when_store_down(settings, tokens: TokenFactory):
    class _BrokenStore(InMemoryStore):
        def get_job(self, job_id: int):
            raise RuntimeError("db down")

    verifier = build_verifier(settings, http_get=tokens.http_get)
    app = create_app(settings=settings, store=_BrokenStore(), verifier=verifier)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503


def test_internal_error_is_masked(settings, tokens: TokenFactory):
    class _ExplodingStore(InMemoryStore):
        def enqueue(self, *a, **k):
            raise RuntimeError("boom with secret db url")

    verifier = build_verifier(settings, http_get=tokens.http_get)
    app = create_app(settings=settings, store=_ExplodingStore(), verifier=verifier)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello"}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert resp.json()["title"] == "Internal server error"


# --- store selection --------------------------------------------------------

def test_defaults_to_in_memory_store_when_no_database_url(settings, tokens: TokenFactory):
    # No injected store + database_url is None -> lifespan builds InMemoryStore.
    verifier = build_verifier(settings, http_get=tokens.http_get)
    app = create_app(settings=settings, verifier=verifier)
    with TestClient(app) as client:  # entering the context runs the lifespan
        assert client.get("/healthz").status_code == 200
        assert isinstance(app.state.store, InMemoryStore)
