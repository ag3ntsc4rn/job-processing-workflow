from __future__ import annotations

import jwt
import pytest

from jobclient.config import Config
from jobclient.tokens import mint_jwt


def _decode(token: str, config: Config, *, verify_exp: bool = True) -> dict:
    return jwt.decode(
        token,
        config.jwt_secret,
        algorithms=[config.jwt_algorithm],
        audience=config.audience,
        issuer=config.issuer,
        options={"verify_exp": verify_exp},
    )


def test_mint_jwt_carries_scopes_space_delimited() -> None:
    config = Config(scopes=("jobs.write", "jobs.read"))
    claims = _decode(mint_jwt(config), config)
    assert claims["scope"] == "jobs.write jobs.read"


def test_mint_jwt_sets_standard_claims_and_ttl() -> None:
    config = Config(
        issuer="autosys",
        subject="autosys-svc",
        audience="job-api",
        jwt_ttl_seconds=120,
    )
    claims = _decode(mint_jwt(config, now=1_000), config, verify_exp=False)
    assert claims["iss"] == "autosys"
    assert claims["sub"] == "autosys-svc"
    assert claims["aud"] == "job-api"
    assert (claims["iat"], claims["nbf"], claims["exp"]) == (1_000, 1_000, 1_120)


def test_mint_jwt_is_unique_per_call() -> None:
    config = Config()
    first = _decode(mint_jwt(config, now=1_000), config, verify_exp=False)
    second = _decode(mint_jwt(config, now=1_000), config, verify_exp=False)
    assert first["jti"] != second["jti"]


def test_expired_token_is_rejected_by_a_verifier() -> None:
    config = Config(jwt_ttl_seconds=60)
    token = mint_jwt(config, now=1_000)
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(token, config)
