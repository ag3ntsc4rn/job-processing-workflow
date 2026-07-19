"""Shared fixtures for the API tests.

Authentication is done upstream by the enterprise JWT auth middleware, so the app
never verifies signatures — it only reads the validated claims. Tests therefore
mint plain (unsigned-payload) tokens carrying whatever claims a test needs; the
app decodes the payload without checking the signature, exactly as it does in
production behind the auth middleware.
"""

from __future__ import annotations

import jwt
import pytest


def make_token(
    *,
    scope: str | None = "jobs.read jobs.write",
    sub: str = "svc",
    client_id: str | None = "svc-app",
    extra: dict | None = None,
) -> str:
    """Mint a token whose payload carries the given claims (signature ignored)."""
    claims: dict = {"sub": sub}
    if client_id is not None:
        claims["client_id"] = client_id
    if scope is not None:
        claims["scope"] = scope
    if extra:
        claims.update(extra)
    # Signature is never checked by the app; any key works. Use a 32+ byte one
    # only to avoid PyJWT's short-key warning.
    return jwt.encode(claims, "x" * 32, algorithm="HS256")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token() -> str:
    return make_token()
