"""JWT resource-server authentication.

The service validates the Bearer JWT it receives on every protected request:

1. select the signing key by ``kid`` from the issuer's JWKS (cached, refreshed
   on rotation),
2. verify signature + ``iss`` / ``aud`` / ``exp`` / ``nbf`` (with clock-skew
   leeway),
3. distill the claims into a :class:`Principal`.

v2 is machine-to-machine only, so the token is always a ``client_credentials``
access token and every principal is a service. Who *signs* the token is pure
config: Keycloak in local dev, the Apigee-minted JWT in prod. Network fetch is
injectable (``http_get``) so the verifier is fully unit-tested with locally
minted keys and no live IdP.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import jwt

from handlerAPIv2.config import Settings
from handlerAPIv2.errors import ProblemException
from handlerAPIv2.principal import Principal

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


class _HttpResponse(Protocol):
    def raise_for_status(self) -> object: ...
    def json(self) -> dict: ...


HttpGet = Callable[[str], _HttpResponse]


def _default_http_get(url: str) -> _HttpResponse:  # pragma: no cover - network I/O
    import httpx

    return httpx.get(url, timeout=5.0)


class JwksCache:
    """Fetches and caches the issuer's JWKS, refreshing on TTL or unknown kid."""

    def __init__(self, jwks_url: str, *, http_get: HttpGet | None = None, ttl: float = 3600.0):
        self._jwks_url = jwks_url
        self._http_get = http_get or _default_http_get
        self._ttl = ttl
        self._lock = threading.Lock()
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        resp = self._http_get(self._jwks_url)
        resp.raise_for_status()
        keyset = jwt.PyJWKSet.from_dict(resp.json())
        self._keys = {k.key_id: k for k in keyset.keys}
        self._fetched_at = time.monotonic()

    def get_key(self, kid: str | None) -> jwt.PyJWK:
        with self._lock:
            stale = (time.monotonic() - self._fetched_at) > self._ttl
            if kid not in self._keys or stale:
                self._refresh()
            if kid not in self._keys:  # possible key rotation since last refresh
                self._refresh()
            key = self._keys.get(kid)
            if key is None:
                raise ProblemException(
                    401, "Unauthorized", "no matching signing key", headers=_BEARER_CHALLENGE
                )
            return key


class TokenVerifier:
    def __init__(self, settings: Settings, jwks: JwksCache) -> None:
        self._settings = settings
        self._jwks = jwks

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as err:
            raise ProblemException(
                401, "Unauthorized", "malformed token", headers=_BEARER_CHALLENGE
            ) from err

        signing_key = self._jwks.get_key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._settings.oidc_algorithms),
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                leeway=self._settings.clock_skew_leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as err:
            raise ProblemException(
                401, "Unauthorized", "invalid token", headers=_BEARER_CHALLENGE
            ) from err
        return self._to_principal(claims)

    def _to_principal(self, claims: dict) -> Principal:
        scopes = _extract_scopes(claims)
        client_id = claims.get("client_id") or claims.get("azp")
        subject = claims.get("sub") or client_id or "unknown"
        return Principal(
            subject=str(subject),
            client_id=client_id,
            scopes=scopes,
        )


def _extract_scopes(claims: dict) -> frozenset[str]:
    # OAuth2 access tokens carry scopes either as a space-delimited "scope"
    # string (RFC standard, Keycloak's default) or a "scp" list (some IdPs).
    scope = claims.get("scope")
    if isinstance(scope, str):
        return frozenset(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, list):
        return frozenset(str(s) for s in scp)
    return frozenset()


def build_verifier(settings: Settings, *, http_get: HttpGet | None = None) -> TokenVerifier:
    jwks_url = settings.oidc_jwks_url or _discover_jwks_url(settings, http_get)
    cache = JwksCache(jwks_url, http_get=http_get, ttl=settings.jwks_cache_ttl)
    return TokenVerifier(settings, cache)


def _discover_jwks_url(settings: Settings, http_get: HttpGet | None) -> str:  # pragma: no cover
    get = http_get or _default_http_get
    well_known = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = get(well_known)
    resp.raise_for_status()
    jwks_uri = resp.json().get("jwks_uri")
    if not jwks_uri:
        raise RuntimeError(f"issuer discovery at {well_known} has no jwks_uri")
    return jwks_uri
