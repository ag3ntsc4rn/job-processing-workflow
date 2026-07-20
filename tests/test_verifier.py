"""Tests for the JWT verifier (AUTH_VERIFY on / prod path).

Mints RS256 tokens with a locally-generated key and serves the matching JWKS to
the verifier via an injected ``http_get``, so the full signature-verification
path is exercised with no live IdP or network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from auth.verifier import JwksCache, build_verifier
from config import Settings
from errors import ProblemException
from main import create_app
from store.memory import InMemoryStore
from tests.conftest import auth

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


class RSATokenFactory:
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
def rsa_tokens() -> RSATokenFactory:
    return RSATokenFactory()


def _settings() -> Settings:
    return Settings(
        database_url=None,
        auth_verify=True,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
    )


def _verifier(tokens: RSATokenFactory):
    return build_verifier(_settings(), http_get=tokens.http_get)


# --- verifier unit tests ----------------------------------------------------

def test_verify_valid_token_yields_service_principal(rsa_tokens: RSATokenFactory):
    principal = _verifier(rsa_tokens).verify(rsa_tokens.mint())
    assert principal.subject == "svc-client"
    assert principal.client_id == "svc-client"
    assert principal.has_scope("jobs.read")
    assert principal.to_creator().type == "service"


def test_verify_rejects_malformed_token(rsa_tokens: RSATokenFactory):
    with pytest.raises(ProblemException) as exc:
        _verifier(rsa_tokens).verify("not-a-jwt")
    assert exc.value.status_code == 401


def test_verify_rejects_expired_token(rsa_tokens: RSATokenFactory):
    with pytest.raises(ProblemException):
        _verifier(rsa_tokens).verify(rsa_tokens.mint(exp_delta=-3600))


def test_verify_rejects_wrong_audience(rsa_tokens: RSATokenFactory):
    with pytest.raises(ProblemException):
        _verifier(rsa_tokens).verify(rsa_tokens.mint(audience="other-api"))


def test_verify_rejects_wrong_issuer(rsa_tokens: RSATokenFactory):
    with pytest.raises(ProblemException):
        _verifier(rsa_tokens).verify(rsa_tokens.mint(issuer="https://evil.example.test"))


def test_verify_rejects_unknown_kid(rsa_tokens: RSATokenFactory):
    with pytest.raises(ProblemException):
        _verifier(rsa_tokens).verify(rsa_tokens.mint(kid="no-such-key"))


def test_jwks_cache_refreshes_and_rejects_unknown_kid(rsa_tokens: RSATokenFactory):
    calls = {"n": 0}

    def counting_get(url: str):
        calls["n"] += 1
        return rsa_tokens.http_get(url)

    cache = JwksCache(JWKS_URL, http_get=counting_get, ttl=3600)
    assert cache.get_key(KID) is not None
    assert calls["n"] == 1
    cache.get_key(KID)  # cached, no extra fetch
    assert calls["n"] == 1

    with pytest.raises(ProblemException):
        cache.get_key("unknown-kid")
    assert calls["n"] == 3  # refreshes twice trying to find the unknown kid


# --- end-to-end: verify path enforced through the API -----------------------

def _client(rsa_tokens: RSATokenFactory) -> TestClient:
    app = create_app(settings=_settings(), store=InMemoryStore(), verifier=_verifier(rsa_tokens))
    return TestClient(app)


def test_api_accepts_properly_signed_token(rsa_tokens: RSATokenFactory):
    resp = _client(rsa_tokens).post(
        "/v1/jobs", json={"job_type": "hello"}, headers=auth(rsa_tokens.mint())
    )
    assert resp.status_code == 201


def test_api_rejects_forged_token_when_verifying(rsa_tokens: RSATokenFactory):
    # A token signed by a *different* key (an attacker's) with the right scopes.
    forged = RSATokenFactory().mint(scope="jobs.write")
    resp = _client(rsa_tokens).post(
        "/v1/jobs", json={"job_type": "hello"}, headers=auth(forged)
    )
    assert resp.status_code == 401
