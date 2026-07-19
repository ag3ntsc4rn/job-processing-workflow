"""FastAPI application factory.

``create_app`` wires settings, middleware, auth, and routes. The store and JWT
verifier are created lazily in the lifespan for real runs but can be injected
(tests, alternative stores) so the app needs no database or live IdP to exercise
the HTTP layer.

**Store selection** mirrors the demo-to-prod path: when ``settings.database_url``
is unset the app runs on the process-local :class:`~store.memory.InMemoryStore`;
set it and the real :class:`~store.postgres.PostgresStore` takes over with no code
change.

Edge concerns (rate limiting, CORS, TLS) are owned by the API gateway (Apigee),
so they are deliberately absent here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.middleware import SecurityMiddleware
from api.routes import router
from auth.verifier import TokenVerifier, build_verifier
from config import Settings
from errors import register_error_handlers
from store.base import Store
from store.memory import InMemoryStore


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    verifier: TokenVerifier | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    injected_store = store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        created_store = False
        if injected_store is None:
            if settings.database_url:  # pragma: no cover - real DB path
                from store.postgres import PostgresStore

                app.state.store = PostgresStore(settings.database_url)
            else:
                # Demo / no infra: process-local store. Same enqueue semantics;
                # PostgresStore takes over untouched once DATABASE_URL is set.
                app.state.store = InMemoryStore()
            created_store = True
        if getattr(app.state, "verifier", None) is None:  # pragma: no cover - real IdP path
            app.state.verifier = build_verifier(settings)
        try:
            yield
        finally:
            if created_store and hasattr(app.state.store, "close"):
                app.state.store.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    if store is not None:
        app.state.store = store
    if verifier is not None:
        app.state.verifier = verifier

    app.add_middleware(SecurityMiddleware)

    register_error_handlers(app)
    app.include_router(router)
    return app
