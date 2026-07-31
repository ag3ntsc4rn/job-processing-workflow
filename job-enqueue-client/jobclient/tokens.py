"""Local JWT minting — the one seam an OAuth2 token provider will replace."""

from __future__ import annotations

import time
import uuid

import jwt

from jobclient.config import Config


def mint_jwt(config: Config, *, now: int | None = None) -> str:
    """Return a signed JWT whose ``scope`` claim carries the configured scopes.

    Claims follow the shape the API will eventually receive from the gateway
    (``iss``/``sub``/``aud``/``iat``/``exp``/``jti`` plus a space-delimited
    ``scope``, RFC 6749 §3.3), so the request looks identical before and after
    real tokens arrive. Nothing verifies this signature today.
    """
    issued_at = int(time.time()) if now is None else now
    claims = {
        "iss": config.issuer,
        "sub": config.subject,
        "aud": config.audience,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + config.jwt_ttl_seconds,
        "jti": uuid.uuid4().hex,
        "scope": " ".join(config.scopes),
    }
    return jwt.encode(claims, config.jwt_secret, algorithm=config.jwt_algorithm)
