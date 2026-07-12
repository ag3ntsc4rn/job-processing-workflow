"""In-app rate limiting (slowapi).

Keyed by the caller's bearer credential (a hash of the token, so distinct
clients get distinct buckets and one noisy client can't starve others) and
falling back to client IP for unauthenticated hits. This runs as ASGI
middleware — before route dependencies — so it keys off the raw token rather
than the decoded principal. This is the in-app layer; a shared API gateway / LB
limiter is the intended eventual home, at which point this stays as defence in
depth.
"""

from __future__ import annotations

import hashlib

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from handlerAPI.config import Settings
from handlerAPI.errors import PROBLEM_MEDIA_TYPE


def _key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        digest = hashlib.sha256(token.strip().encode()).hexdigest()[:32]
        return f"tok:{digest}"
    return get_remote_address(request)


def build_limiter(settings: Settings) -> Limiter:
    return Limiter(
        key_func=_key,
        default_limits=[settings.rate_limit],
        enabled=settings.rate_limit_enabled,
        headers_enabled=True,
    )


def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        media_type=PROBLEM_MEDIA_TYPE,
        content={
            "type": "about:blank",
            "title": "Too Many Requests",
            "status": 429,
            "detail": f"rate limit exceeded: {exc.limit.limit}",
            "instance": str(request.url.path),
        },
    )
