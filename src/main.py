"""Application factory + entrypoint.

``create_app`` wires settings, middleware, and routes and exposes the module-level
``app`` object, so the standard ASGI import string ``main:app`` works:

    export PYTHONPATH=src
    cd src
    python -m uvicorn main:app --host 0.0.0.0 --port 8080

``python -m main`` also works and additionally honours ``HOST``/``PORT`` from the
environment.

**Auth** is chosen by ``AUTH_VERIFY``: on (default / prod) the app re-verifies
the JWT signature + ``iss`` / ``aud`` / ``exp`` against the issuer's JWKS (a
verifier is built at startup and stashed on ``app.state.verifier``); off (dev)
skips verification and reads claims from the token payload so a developer can
craft their own token. Either way routes enforce per-endpoint scopes (see
``api/deps``).

**Store selection** mirrors the demo-to-prod path: when ``settings.database_url``
is unset the app runs on the process-local :class:`~store.memory.InMemoryStore`;
set it and the real :class:`~store.postgres.PostgresStore` takes over with no code
change. Edge concerns (rate limiting, CORS, TLS) are owned by the API gateway
(Apigee), so they are deliberately absent here.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.middleware import SecurityMiddleware
from api.routes import router
from auth.verifier import TokenVerifier, build_verifier
from config import Settings
from errors import register_error_handlers
from store.base import Store
from store.memory import InMemoryStore

logger = logging.getLogger("job-api")


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
        # Only a store we create here is ours to close; an injected one is not.
        owned_store: Store | None = None
        if injected_store is None:
            if settings.database_url:  # pragma: no cover - real DB path
                from store.postgres import PostgresStore

                owned_store = PostgresStore(settings.database_url)
            else:
                # Demo / no infra: process-local store. Same enqueue semantics;
                # PostgresStore takes over untouched once DATABASE_URL is set.
                owned_store = InMemoryStore()
            app.state.store = owned_store
        # Build the JWT verifier lazily at startup so importing this module has
        # no side effects (no JWKS fetch / issuer discovery).
        if settings.auth_verify and getattr(app.state, "verifier", None) is None:
            app.state.verifier = build_verifier(settings)  # pragma: no cover - real IdP
        try:
            yield
        finally:
            # close() is optional on the Store protocol (PostgresStore has it,
            # InMemoryStore doesn't), so probe for it rather than assume.
            close = getattr(owned_store, "close", None)
            if callable(close):  # pragma: no cover - real DB path
                close()

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

    if not settings.auth_verify:
        logger.warning(
            "AUTH_VERIFY is off: JWT signatures are NOT verified and claims are "
            "read from the token payload. Use only in local development."
        )

    app.add_middleware(SecurityMiddleware)

    register_error_handlers(app)
    app.include_router(router)
    return app


# Module-level ASGI app for the standard `uvicorn main:app` invocation. Building
# it is cheap — settings are read from the environment and the store is created
# only when the lifespan runs — so importing this module is side-effect-free.
app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    settings = Settings.from_env()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
