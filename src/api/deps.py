"""FastAPI dependencies: settings, store, authenticated principal, scope guards.

The store and token verifier are process singletons created in the app lifespan
and stashed on ``app.state``; these dependencies read them from the request so
routes stay thin and tests can swap them via ``app.dependency_overrides`` or by
building the app with injected components.

**Adding an endpoint that needs a new scope** is two explicit steps, both in
code — grant the scope to the client in your IdP, then guard the route here:

    require_cancel = require_scope(lambda s: s.scope_cancel)   # env-named, or
    require_cancel = require_scope(lambda _s: "jobs.cancel")   # literal policy

The scope *name* may come from settings (so ops can rename it per environment);
*which endpoint requires it* is authorization policy and stays in code, never
inferred from whatever scopes happen to be in the token.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from auth.principal import Principal
from auth.verifier import TokenVerifier
from config import Settings
from errors import ProblemException
from store.base import Store

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

ScopeSelector = Callable[[Settings], str]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier


def get_principal(
    request: Request,
    verifier: TokenVerifier = Depends(get_verifier),
) -> Principal:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ProblemException(
            401,
            "Unauthorized",
            "missing bearer token",
            headers=_BEARER_CHALLENGE,
        )
    principal = verifier.verify(token.strip())
    request.state.principal = principal  # for logging / correlation
    return principal


def require_scope(select: ScopeSelector) -> Callable[..., Principal]:
    """Build a dependency that 403s unless the caller holds the selected scope.

    ``select`` resolves the required scope name from settings at request time, so
    scope strings stay configurable while the endpoint->scope mapping stays
    explicit in code.
    """

    def guard(
        principal: Principal = Depends(get_principal),
        settings: Settings = Depends(get_settings),
    ) -> Principal:
        required = select(settings)
        if not principal.has_scope(required):
            raise ProblemException(403, "Forbidden", f"requires scope: {required}")
        return principal

    return guard


require_write = require_scope(lambda s: s.scope_write)
require_read = require_scope(lambda s: s.scope_read)
