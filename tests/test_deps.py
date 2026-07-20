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
from config import Settings
from errors import register_error_handlers
from tests.conftest import auth, make_token

require_cancel = require_scope(lambda _s: "jobs.cancel")


def _app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings or Settings(database_url=None)
    register_error_handlers(app)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: int, principal: Principal = Depends(require_cancel)) -> dict[str, str]:
        return {"cancelled": str(job_id)}

    return app


def test_require_scope_allows_when_scope_present():
    client = TestClient(_app())
    token = make_token(scope="jobs.read jobs.cancel")
    resp = client.post("/v1/jobs/7/cancel", headers=auth(token))
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": "7"}


def test_require_scope_forbids_when_scope_missing():
    client = TestClient(_app())
    token = make_token(scope="jobs.read jobs.write")  # no jobs.cancel
    resp = client.post("/v1/jobs/7/cancel", headers=auth(token))
    assert resp.status_code == 403
    assert "jobs.cancel" in resp.text


def test_require_scope_selects_name_from_settings():
    # A settings-driven selector resolves the scope string at request time.
    guard = require_scope(lambda s: s.scope_write)
    app = FastAPI()
    app.state.settings = Settings(database_url=None, scope_write="jobs.admin")
    register_error_handlers(app)

    @app.get("/admin")
    def admin(principal: Principal = Depends(guard)) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/admin", headers=auth(make_token(scope="jobs.admin"))).status_code == 200
    assert client.get("/admin", headers=auth(make_token(scope="jobs.read"))).status_code == 403


def test_store_dedup_returns_none_on_active():
    from store.memory import InMemoryStore

    store = InMemoryStore()
    assert store.enqueue("hello") == 1
    assert store.enqueue("hello") is None  # active job of this type exists
    job = store.get_job(1)
    assert job is not None
    assert job.job_type == "hello"
    assert store.get_job(999) is None
