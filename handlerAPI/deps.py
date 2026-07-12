"""FastAPI dependencies: settings, store, authenticated principal, scope guards.

The store and token verifier are process singletons created in the app lifespan
and stashed on ``app.state``; these dependencies read them from the request so
routes stay thin and tests can swap them via ``app.dependency_overrides`` or by
building the app with injected components.
"""

from __future__ import annotations

from fastapi import Depends, Request

from common.store import Store
from handlerAPI.auth import TokenVerifier
from handlerAPI.config import Settings
from handlerAPI.errors import ProblemException
from handlerAPI.principal import Principal

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


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


def require_write(
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not principal.has_scope(settings.scope_write):
        raise ProblemException(403, "Forbidden", f"requires scope: {settings.scope_write}")
    return principal


def require_read(
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not principal.has_scope(settings.scope_read):
        raise ProblemException(403, "Forbidden", f"requires scope: {settings.scope_read}")
    return principal


def can_read_job_created_by(
    principal: Principal, settings: Settings, creator_sub: str | None
) -> bool:
    """Ownership rule for reads: services and holders of the read-all scope can
    read any job; a human user can only read jobs they created."""
    if principal.is_service or principal.has_scope(settings.scope_read_all):
        return True
    return creator_sub is not None and creator_sub == principal.subject
