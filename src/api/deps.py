"""FastAPI dependencies: settings, store, authenticated principal, scope guards.

Authentication (signature / issuer / audience / expiry) is handled *upstream* by
the enterprise JWT auth middleware (wired in ``main.py`` via
``add_jwt_auth(app, exclude_routes=[...])``). By the time a request reaches a
route, the token is already validated — so this module never verifies a token or
touches JWKS/OIDC. It only reads the validated claims and enforces per-endpoint
scope authorization, which the auth middleware does not do.

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

import jwt
from fastapi import Depends, Request

from auth.principal import Principal
from config import Settings
from errors import ProblemException
from store.base import Store

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

ScopeSelector = Callable[[Settings], str]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> Store:
    return request.app.state.store


def _claims_from_request(request: Request) -> dict | None:
    """Return the validated JWT claims for this request, or ``None`` if absent.

    The enterprise auth middleware has already validated the token upstream, so
    we decode the Bearer payload *without* re-verifying the signature (no JWKS,
    no OIDC config needed) purely to read the claims.

    If your middleware instead exposes the decoded claims directly, swap the body
    for e.g. ``return getattr(request.state, "claims", None)``.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        return jwt.decode(token.strip(), options={"verify_signature": False})
    except jwt.PyJWTError:
        return None


def get_principal(request: Request) -> Principal:
    claims = _claims_from_request(request)
    if claims is None:
        raise ProblemException(
            401,
            "Unauthorized",
            "missing authenticated identity",
            headers=_BEARER_CHALLENGE,
        )
    principal = Principal.from_claims(claims)
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
