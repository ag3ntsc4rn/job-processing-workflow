"""Application factory + entrypoint.

``create_app`` wires settings, middleware, and routes and exposes the module-level
``app`` object, so the standard ASGI import string ``main:app`` works:

    export PYTHONPATH=src
    cd src
    python -m uvicorn main:app --host 0.0.0.0 --port 8080

``python -m main`` also works and additionally honours ``HOST``/``PORT`` from the
environment.

**Auth is upstream.** Token validation (signature / issuer / audience / expiry)
is done by the enterprise JWT auth middleware — wire it in ``create_app`` at the
marked spot with ``add_jwt_auth(app, exclude_routes=[...])``. The routes only
read the validated claims and enforce scopes (see ``api/deps``).

**Store selection** mirrors the demo-to-prod path: when ``settings.database_url``
is unset the app runs on the process-local :class:`~store.memory.InMemoryStore`;
set it and the real :class:`~store.postgres.PostgresStore` takes over with no code
change. Edge concerns (rate limiting, CORS, TLS) are owned by the API gateway
(Apigee), so they are deliberately absent here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.middleware import SecurityMiddleware
from api.routes import router
from config import Settings
from errors import register_error_handlers
from store.base import Store
from store.memory import InMemoryStore


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
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

    # --- Enterprise JWT auth middleware goes here (prod) ---
    # The token is validated upstream; routes only read claims + enforce scopes.
    #   from your_company.auth import add_jwt_auth
    #   add_jwt_auth(app, exclude_routes=["/healthz", "/readyz", "/docs",
    #                                     "/openapi.json"])
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
