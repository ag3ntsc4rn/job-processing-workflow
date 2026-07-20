"""Unit tests for principal derivation from validated claims and claim reading.

The token is validated upstream by the enterprise auth middleware; this service
only turns the validated claims into a :class:`Principal` and reads scopes.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.deps import get_principal
from auth.principal import Principal, extract_scopes
from config import Settings
from errors import register_error_handlers
from tests.conftest import auth, make_token


def test_service_principal_from_client_credentials():
    principal = Principal.from_claims(
        {"sub": "svc", "client_id": "svc-app", "scope": "jobs.write jobs.read"}
    )
    assert principal.subject == "svc"
    assert principal.client_id == "svc-app"
    assert principal.has_scope("jobs.write")
    assert principal.has_scope("jobs.read")
    assert principal.to_creator().type == "service"


def test_client_id_falls_back_to_azp():
    principal = Principal.from_claims({"sub": "svc", "azp": "gateway-app"})
    assert principal.client_id == "gateway-app"


def test_subject_falls_back_to_client_id_then_unknown():
    assert Principal.from_claims({"client_id": "only-client"}).subject == "only-client"
    assert Principal.from_claims({}).subject == "unknown"


def test_extract_scopes_from_scope_string():
    assert extract_scopes({"scope": "a b c"}) == frozenset({"a", "b", "c"})


def test_extract_scopes_from_scp_list():
    assert extract_scopes({"scp": ["a", "b"]}) == frozenset({"a", "b"})


def test_extract_scopes_empty():
    assert extract_scopes({}) == frozenset()


def _principal_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(database_url=None)
    register_error_handlers(app)

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)) -> dict:
        return {"sub": principal.subject, "scopes": sorted(principal.scopes)}

    return app


def test_get_principal_reads_claims_from_bearer():
    client = TestClient(_principal_app())
    token = make_token(sub="svc-x", scope="jobs.read")
    resp = client.get("/whoami", headers=auth(token))
    assert resp.status_code == 200
    assert resp.json() == {"sub": "svc-x", "scopes": ["jobs.read"]}


def test_get_principal_401_without_bearer():
    client = TestClient(_principal_app())
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_get_principal_401_on_malformed_token():
    client = TestClient(_principal_app())
    resp = client.get("/whoami", headers=auth("not.a.jwt"))
    assert resp.status_code == 401


def test_from_env_store_toggle(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings.from_env().database_url is None
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@db:5432/app")
    assert Settings.from_env().database_url == "postgresql://app:app@db:5432/app"


def test_auth_verify_defaults_on_and_parses_env(monkeypatch):
    monkeypatch.delenv("AUTH_VERIFY", raising=False)
    assert Settings.from_env().auth_verify is True  # secure by default
    for falsey in ("false", "0", "no", "off"):
        monkeypatch.setenv("AUTH_VERIFY", falsey)
        assert Settings.from_env().auth_verify is False
    for truthy in ("true", "1", "yes", "on"):
        monkeypatch.setenv("AUTH_VERIFY", truthy)
        assert Settings.from_env().auth_verify is True
