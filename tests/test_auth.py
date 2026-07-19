"""Unit tests for the token verifier, principal derivation, and JWKS cache."""

from __future__ import annotations

import pytest

from auth.verifier import JwksCache, _extract_scopes, build_verifier
from config import Settings
from errors import ProblemException
from tests.conftest import ISSUER, JWKS_URL, KID, TokenFactory


def _verifier(tokens: TokenFactory) -> object:
    settings = Settings(
        database_url=None,
        oidc_issuer=ISSUER,
        oidc_audience="job-api",
        oidc_jwks_url=JWKS_URL,
    )
    return build_verifier(settings, http_get=tokens.http_get)


def test_service_principal_from_client_credentials(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(sub="svc", client_id="svc-app", scope="jobs.write jobs.read")
    principal = verifier.verify(token)
    assert principal.subject == "svc"
    assert principal.client_id == "svc-app"
    assert principal.has_scope("jobs.write")
    assert principal.has_scope("jobs.read")
    assert principal.to_creator().type == "service"


def test_client_id_falls_back_to_azp(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(sub="svc", extra={"azp": "gateway-app", "client_id": None})
    principal = verifier.verify(token)
    assert principal.client_id == "gateway-app"


def test_extract_scopes_from_scope_string():
    assert _extract_scopes({"scope": "a b c"}) == frozenset({"a", "b", "c"})


def test_extract_scopes_from_scp_list():
    assert _extract_scopes({"scp": ["a", "b"]}) == frozenset({"a", "b"})


def test_extract_scopes_empty():
    assert _extract_scopes({}) == frozenset()


def test_jwks_cache_refreshes_and_rejects_unknown_kid(tokens: TokenFactory):
    calls = {"n": 0}

    def counting_get(url: str):
        calls["n"] += 1
        return tokens.http_get(url)

    cache = JwksCache(JWKS_URL, http_get=counting_get, ttl=3600)
    assert cache.get_key(KID) is not None
    assert calls["n"] == 1
    cache.get_key(KID)
    assert calls["n"] == 1

    with pytest.raises(ProblemException):
        cache.get_key("unknown-kid")
    assert calls["n"] == 3


def test_from_env_store_toggle(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings.from_env().database_url is None
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@db:5432/app")
    assert Settings.from_env().database_url == "postgresql://app:app@db:5432/app"
