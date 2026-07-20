"""FastAPI dependencies: settings, store, authenticated principal, scope guards.

Authentication has two modes, chosen by ``AUTH_VERIFY`` (see ``config`` and
``main.create_app``):

* **verify on (default / prod)** — a :class:`~auth.verifier.TokenVerifier` is
  built at startup and stashed on ``app.state.verifier``; ``get_principal``
  re-verifies the token's signature + ``iss`` / ``aud`` / ``exp`` against JWKS.
  A forged / self-minted token is rejected with ``401`` even on a direct call
  that bypasses the gateway.
* **verify off (dev)** — no verifier is built; ``get_principal`` reads claims
  straight from the token payload, so a developer can craft their own token.

Either way a Bearer token is required (no token -> ``401``) and per-endpoint
scope authorization runs the same.

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


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def get_principal(request: Request) -> Principal:
    token = _bearer_token(request)
    if token is None:
        raise ProblemException(
            401, "Unauthorized", "missing bearer token", headers=_BEARER_CHALLENGE
        )

    verifier: TokenVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is not None:
        # prod: re-verify signature + iss/aud/exp; raises 401 on any failure.
        principal = verifier.verify(token)
    else:
        # dev (AUTH_VERIFY off): read claims from the payload without verifying.
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as err:
            raise ProblemException(
                401, "Unauthorized", "malformed token", headers=_BEARER_CHALLENGE
            ) from err
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
