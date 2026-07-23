"""FastAPI application factory.

``create_app`` wires settings, middleware, auth, and routes. The store and JWT
verifier are created lazily in the lifespan for real runs but can be injected
(tests, alternative stores) so the app needs no database or live IdP to exercise
the HTTP layer.

**Store selection** mirrors what the operator wants for a demo-to-prod path: when
``settings.database_url`` is unset the app runs on the process-local
:class:`~common.store.InMemoryStore`; set it and the real
:class:`~common.db.PostgresStore` takes over with no code change.

Edge concerns (rate limiting, CORS, TLS) are owned by the API gateway (Apigee),
so they are deliberately absent here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.store import InMemoryStore, Store
from handlerAPIv2.auth import TokenVerifier, build_verifier
from handlerAPIv2.config import Settings
from handlerAPIv2.errors import register_error_handlers
from handlerAPIv2.middleware import SecurityMiddleware
from handlerAPIv2.routes import router


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
                from common.db import PostgresStore
                from handlerAPIv2.circuit_breaker import (
                    CircuitBreakerStore,
                    build_breaker,
                )

                # Guard the DB behind a circuit breaker so a sustained Postgres
                # outage fails fast (503) instead of exhausting the pool.
                app.state.store = CircuitBreakerStore(
                    PostgresStore(settings.database_url),
                    build_breaker(
                        failure_threshold=settings.db_circuit_failure_threshold,
                        reset_timeout=settings.db_circuit_reset_timeout,
                    ),
                )
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
