"""Shared fixtures for the API tests.

Mints RS256 tokens with a locally-generated key and serves the matching JWKS to
the verifier via an injected ``http_get``, so the full auth path is exercised
with no live IdP or network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://issuer.example.test"
AUDIENCE = "job-api"
JWKS_URL = "https://issuer.example.test/jwks"
KID = "test-key-1"


@dataclass
class _FakeResponse:
    _body: dict

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class TokenFactory:
    """Generates an RSA key, exposes its JWKS, and mints signed tokens."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}

    def http_get(self, url: str) -> _FakeResponse:
        return _FakeResponse(self.jwks)

    def mint(
        self,
        *,
        scope: str = "jobs.read jobs.write",
        sub: str = "svc-client",
        client_id: str = "svc-client",
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        exp_delta: int = 300,
        kid: str = KID,
        extra: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": sub,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": now + exp_delta,
        }
        if extra:
            claims.update(extra)
        return jwt.encode(claims, self._pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def tokens() -> TokenFactory:
    return TokenFactory()
