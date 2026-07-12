"""Unit tests for token validation, principal derivation, and the JWKS cache."""

from __future__ import annotations

import pytest

from handlerAPI.auth import JwksCache, _extract_scopes, build_verifier
from handlerAPI.config import Settings
from handlerAPI.errors import ProblemException
from tests.conftest import ISSUER, JWKS_URL, KID, TokenFactory


def _verifier(tokens: TokenFactory) -> object:
    settings = Settings(
        database_url="",
        oidc_issuer=ISSUER,
        oidc_audience="job-api",
        oidc_jwks_url=JWKS_URL,
    )
    return build_verifier(settings, http_get=tokens.http_get)


def test_service_principal_detected(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(sub="svc", client_id="svc", scope="jobs.write")
    principal = verifier.verify(token)
    assert principal.principal_type == "service"
    assert principal.is_service
    assert principal.client_id == "svc"
    assert principal.has_scope("jobs.write")


def test_user_principal_detected_via_email_claim(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(sub="user-9", client_id="spa", extra={"email": "u@x.test"})
    principal = verifier.verify(token)
    assert principal.principal_type == "user"
    assert not principal.is_service


def test_user_principal_detected_when_sub_differs_from_client(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(sub="user-9", client_id="spa")
    principal = verifier.verify(token)
    assert principal.principal_type == "user"


def test_groups_claim_captured(tokens: TokenFactory):
    verifier = _verifier(tokens)
    token = tokens.mint(extra={"groups": ["job-admins", "readers"]})
    principal = verifier.verify(token)
    assert principal.in_group("job-admins")
    assert "readers" in principal.groups


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
    assert cache.get_key(KID) is not None  # first refresh
    assert calls["n"] == 1
    cache.get_key(KID)  # cached, no extra fetch
    assert calls["n"] == 1

    with pytest.raises(ProblemException):
        cache.get_key("unknown-kid")  # two refresh attempts, then reject
    assert calls["n"] == 3
