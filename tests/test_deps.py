"""Tests for the reusable scope guard (``require_scope``).

The guard is how future endpoints get authorized: pick a scope (from settings or
a literal) and attach ``Depends(require_scope(...))`` to the route. Here we mount
a throwaway route guarded by a brand-new ``jobs.cancel`` scope to prove the
pattern works without touching the real routes.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.deps import require_scope
from auth.principal import Principal
from auth.verifier import build_verifier
from config import Settings
from errors import register_error_handlers
from tests.conftest import AUDIENCE, ISSUER, JWKS_URL, TokenFactory

require_cancel = require_scope(lambda _s: "jobs.cancel")


def _app(tokens: TokenFactory) -> FastAPI:
    settings = Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.verifier = build_verifier(settings, http_get=tokens.http_get)
    register_error_handlers(app)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: int, principal: Principal = Depends(require_cancel)) -> dict[str, str]:
        return {"cancelled": str(job_id)}

    return app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_require_scope_allows_when_scope_present(tokens: TokenFactory):
    client = TestClient(_app(tokens))
    token = tokens.mint(scope="jobs.read jobs.cancel")
    resp = client.post("/v1/jobs/7/cancel", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": "7"}


def test_require_scope_forbids_when_scope_missing(tokens: TokenFactory):
    client = TestClient(_app(tokens))
    token = tokens.mint(scope="jobs.read jobs.write")  # no jobs.cancel
    resp = client.post("/v1/jobs/7/cancel", headers=_auth(token))
    assert resp.status_code == 403
    assert "jobs.cancel" in resp.text


def test_require_scope_selects_name_from_settings(tokens: TokenFactory):
    # A settings-driven selector resolves the scope string at request time.
    guard = require_scope(lambda s: s.scope_write)
    app = FastAPI()
    settings = Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
        scope_write="jobs.admin",
    )
    app.state.settings = settings
    app.state.verifier = build_verifier(settings, http_get=tokens.http_get)
    register_error_handlers(app)

    @app.get("/admin")
    def admin(principal: Principal = Depends(guard)) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/admin", headers=_auth(tokens.mint(scope="jobs.admin"))).status_code == 200
    assert client.get("/admin", headers=_auth(tokens.mint(scope="jobs.read"))).status_code == 403


def test_store_dedup_returns_none_on_active(tokens: TokenFactory):
    from store.memory import InMemoryStore

    store = InMemoryStore()
    assert store.enqueue("hello") == 1
    assert store.enqueue("hello") is None  # active job of this type exists
    assert store.get_job(1).job_type == "hello"
    assert store.get_job(999) is None
