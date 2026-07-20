"""handlerAPIv2 HTTP tests: authn, authz (scopes), validation, dedup, hardening.

v2 is machine-to-machine only: every caller is a service and reads are not
ownership-gated, so any holder of ``jobs.read`` can read any job (contrast with
v1). Tokens are minted locally (see ``tests/conftest.py``) so the full JWT path
runs with no live Keycloak.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.store import InMemoryStore
from handlerAPIv2.app import create_app
from handlerAPIv2.auth import build_verifier
from handlerAPIv2.config import Settings
from tests.conftest import AUDIENCE, ISSUER, JWKS_URL, TokenFactory


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _svc_token(
    tokens: TokenFactory, *, scope: str = "jobs.read jobs.write", sub: str = "svc"
) -> str:
    return tokens.mint(scope=scope, sub=sub, client_id="svc-app")


@pytest.fixture
def v2_settings() -> Settings:
    return Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
    )


@pytest.fixture
def v2_store() -> InMemoryStore:
    s = InMemoryStore()
    s.set_type_config("hello", payload={"name": "Ada"})
    s.set_type_config("settlement", payload={"region": "us-east-1"})
    return s


@pytest.fixture
def v2_client(
    v2_settings: Settings, v2_store: InMemoryStore, tokens: TokenFactory
) -> TestClient:
    verifier = build_verifier(v2_settings, http_get=tokens.http_get)
    app = create_app(settings=v2_settings, store=v2_store, verifier=verifier)
    return TestClient(app)


# --- authentication (authn) -------------------------------------------------

def test_post_requires_bearer(v2_client: TestClient):
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_post_rejects_malformed_token(v2_client: TestClient):
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth("not-a-jwt"))
    assert resp.status_code == 401


def test_post_rejects_expired_token(v2_client: TestClient, tokens: TokenFactory):
    token = tokens.mint(exp_delta=-3600)
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_wrong_audience(v2_client: TestClient, tokens: TokenFactory):
    token = tokens.mint(audience="some-other-api")
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_wrong_issuer(v2_client: TestClient, tokens: TokenFactory):
    token = tokens.mint(issuer="https://evil.example.test")
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


def test_post_rejects_unknown_kid(v2_client: TestClient, tokens: TokenFactory):
    token = tokens.mint(kid="no-such-key")
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 401


# --- authorization (authz / scopes) -----------------------------------------

def test_post_requires_write_scope(v2_client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens, scope="jobs.read")  # read-only client
    resp = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 403


def test_get_requires_read_scope(v2_client: TestClient, tokens: TokenFactory):
    write = _svc_token(tokens, scope="jobs.write")
    created = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(write))
    job_id = created.json()["job_id"]
    resp = v2_client.get(f"/v1/jobs/{job_id}", headers=_auth(write))
    assert resp.status_code == 403


# --- create + dedup ---------------------------------------------------------

def test_create_job_success_records_service_creator(v2_client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens)
    resp = v2_client.post(
        "/v1/jobs", json={"job_type": "hello", "payload": {"x": 1}}, headers=_auth(token)
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"
    assert resp.headers["Location"] == f"/v1/jobs/{job_id}"

    got = v2_client.get(f"/v1/jobs/{job_id}", headers=_auth(token)).json()
    assert got["input_payload"] == {"x": 1}
    assert got["created_by"] == {"sub": "svc", "type": "service", "client_id": "svc-app"}


def test_create_job_dedup_conflict(v2_client: TestClient, tokens: TokenFactory):
    token = _svc_token(tokens)
    first = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert first.status_code == 201
    second = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert second.status_code == 409


# --- validation -------------------------------------------------------------

def test_create_job_rejects_bad_job_type(v2_client: TestClient, tokens: TokenFactory):
    resp = v2_client.post(
        "/v1/jobs", json={"job_type": "Bad Type!"}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 422


def test_create_job_rejects_unknown_fields(v2_client: TestClient, tokens: TokenFactory):
    resp = v2_client.post(
        "/v1/jobs", json={"job_type": "hello", "bogus": 1}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 422


# --- reads (no ownership in v2) ---------------------------------------------

def test_any_service_can_read_any_job(v2_client: TestClient, tokens: TokenFactory):
    creator = _svc_token(tokens, sub="svc-a")
    created = v2_client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(creator))
    job_id = created.json()["job_id"]

    other = _svc_token(tokens, sub="svc-b")  # different principal, still 200 in v2
    got = v2_client.get(f"/v1/jobs/{job_id}", headers=_auth(other))
    assert got.status_code == 200
    assert got.json()["created_by"]["type"] == "service"


def test_get_missing_job_returns_404(v2_client: TestClient, tokens: TokenFactory):
    resp = v2_client.get("/v1/jobs/999999", headers=_auth(_svc_token(tokens)))
    assert resp.status_code == 404


# --- ops + hardening --------------------------------------------------------

def test_healthz(v2_client: TestClient):
    resp = v2_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz(v2_client: TestClient):
    resp = v2_client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_security_headers_present(v2_client: TestClient):
    resp = v2_client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in resp.headers
    assert "strict-transport-security" in resp.headers
    assert resp.headers.get("x-request-id")


def test_unknown_path_returns_problem_404(v2_client: TestClient):
    resp = v2_client.get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_readyz_reports_unavailable_when_store_down(v2_settings, tokens: TokenFactory):
    class _BrokenStore(InMemoryStore):
        def get_job(self, job_id: int):
            raise RuntimeError("db down")

    verifier = build_verifier(v2_settings, http_get=tokens.http_get)
    app = create_app(settings=v2_settings, store=_BrokenStore(), verifier=verifier)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503


def test_internal_error_is_masked(v2_settings, tokens: TokenFactory):
    class _ExplodingStore(InMemoryStore):
        def enqueue(self, *a, **k):
            raise RuntimeError("boom with secret db url")

    verifier = build_verifier(v2_settings, http_get=tokens.http_get)
    app = create_app(settings=v2_settings, store=_ExplodingStore(), verifier=verifier)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello"}, headers=_auth(_svc_token(tokens))
    )
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert resp.json()["title"] == "Internal server error"


# --- store selection --------------------------------------------------------

def test_defaults_to_in_memory_store_when_no_database_url(
    v2_settings, tokens: TokenFactory
):
    # No injected store + database_url is None -> lifespan builds InMemoryStore.
    verifier = build_verifier(v2_settings, http_get=tokens.http_get)
    app = create_app(settings=v2_settings, verifier=verifier)
    with TestClient(app) as client:  # entering the context runs the lifespan
        assert client.get("/healthz").status_code == 200
        assert isinstance(app.state.store, InMemoryStore)
