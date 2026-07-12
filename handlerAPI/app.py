"""FastAPI application factory.

``create_app`` wires settings, middleware, auth, and routes. The Postgres store
and OIDC verifier are created lazily in the lifespan for real runs, but can be
injected (tests, alternative stores) so the app needs no database or live IdP to
exercise the HTTP layer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from common.store import Store
from handlerAPI.auth import TokenVerifier, build_verifier
from handlerAPI.config import Settings
from handlerAPI.errors import register_error_handlers
from handlerAPI.middleware import SecurityMiddleware
from handlerAPI.ratelimit import build_limiter, rate_limit_handler
from handlerAPI.routes import router


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
        if injected_store is None:  # pragma: no cover - real DB path
            from common.db import PostgresStore

            app.state.store = PostgresStore(settings.database_url)
            created_store = True
        if getattr(app.state, "verifier", None) is None:  # pragma: no cover - real IdP path
            app.state.verifier = build_verifier(settings)
        try:
            yield
        finally:
            if created_store:
                app.state.store.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.limiter = build_limiter(settings)
    if store is not None:
        app.state.store = store
    if verifier is not None:
        app.state.verifier = verifier

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(SecurityMiddleware)

    register_error_handlers(app)
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    app.include_router(router)
    return app
