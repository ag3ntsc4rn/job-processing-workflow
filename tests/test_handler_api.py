"""HTTP API tests: authn, authz/ownership, validation, dedup, hardening."""

from __future__ import annotations

from fastapi.testclient import TestClient

from common.store import InMemoryStore
from handlerAPI.app import create_app
from handlerAPI.auth import build_verifier
from handlerAPI.config import Settings
from tests.conftest import TokenFactory, service_token, user_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- authentication ---------------------------------------------------------

def test_post_requires_bearer(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_post_rejects_malformed_token(client: TestClient):
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth("not-a-jwt"))
    assert resp.status_code == 401


def test_post_rejects_expired_token(client: TestClient, tokens: TokenFactory):
    token = tokens.mint(exp_delta=-3600)  # well past the clock-skew leeway
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


# --- authorization (scopes) -------------------------------------------------

def test_post_requires_write_scope(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens, scope="jobs.read")
    resp = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert resp.status_code == 403


def test_get_requires_read_scope(client: TestClient, tokens: TokenFactory):
    write = service_token(tokens, scope="jobs.write")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(write))
    job_id = created.json()["job_id"]
    resp = client.get(f"/v1/jobs/{job_id}", headers=_auth(write))
    assert resp.status_code == 403


# --- create + dedup ---------------------------------------------------------

def test_create_job_success_records_creator(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "payload": {"x": 1}}, headers=_auth(token)
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"
    assert resp.headers["Location"] == f"/v1/jobs/{job_id}"

    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(token)).json()
    assert got["input_payload"] == {"x": 1}
    assert got["created_by"] == {
        "sub": "svc-client",
        "type": "service",
        "client_id": "svc-client",
    }


def test_create_job_dedup_conflict(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens)
    first = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert first.status_code == 201
    second = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    assert second.status_code == 409


# --- validation -------------------------------------------------------------

def test_create_job_rejects_bad_job_type(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens)
    resp = client.post("/v1/jobs", json={"job_type": "Bad Type!"}, headers=_auth(token))
    assert resp.status_code == 422


def test_create_job_rejects_unknown_fields(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens)
    resp = client.post(
        "/v1/jobs", json={"job_type": "hello", "bogus": 1}, headers=_auth(token)
    )
    assert resp.status_code == 422


# --- read ownership ---------------------------------------------------------

def test_user_can_read_own_job(client: TestClient, tokens: TokenFactory):
    token = user_token(tokens, sub="user-1")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(token))
    job_id = created.json()["job_id"]
    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["created_by"]["type"] == "user"


def test_user_cannot_read_others_job(client: TestClient, tokens: TokenFactory):
    creator = user_token(tokens, sub="user-1")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(creator))
    job_id = created.json()["job_id"]

    other = user_token(tokens, sub="user-2")
    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(other))
    assert got.status_code == 404  # hidden, not 403


def test_service_can_read_any_job(client: TestClient, tokens: TokenFactory):
    creator = user_token(tokens, sub="user-1")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(creator))
    job_id = created.json()["job_id"]

    svc = service_token(tokens)
    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(svc))
    assert got.status_code == 200


def test_user_with_read_all_can_read_others_job(client: TestClient, tokens: TokenFactory):
    creator = user_token(tokens, sub="user-1")
    created = client.post("/v1/jobs", json={"job_type": "hello"}, headers=_auth(creator))
    job_id = created.json()["job_id"]

    admin = user_token(tokens, sub="admin", scope="jobs.read jobs.read.all")
    got = client.get(f"/v1/jobs/{job_id}", headers=_auth(admin))
    assert got.status_code == 200


def test_get_missing_job_returns_404(client: TestClient, tokens: TokenFactory):
    token = service_token(tokens)
    resp = client.get("/v1/jobs/999999", headers=_auth(token))
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
        "/v1/jobs", json={"job_type": "hello"}, headers=_auth(service_token(tokens))
    )
    assert resp.status_code == 500
    assert "secret" not in resp.text  # internals never leak
    assert resp.json()["title"] == "Internal server error"


def test_rate_limit_returns_429(settings, store, tokens: TokenFactory):
    limited = Settings(
        database_url="",
        oidc_issuer=settings.oidc_issuer,
        oidc_audience=settings.oidc_audience,
        oidc_jwks_url=settings.oidc_jwks_url,
        rate_limit="2/minute",
    )
    verifier = build_verifier(limited, http_get=tokens.http_get)
    app = create_app(settings=limited, store=store, verifier=verifier)
    client = TestClient(app)
    codes = [client.get("/healthz").status_code for _ in range(4)]
    assert 429 in codes
