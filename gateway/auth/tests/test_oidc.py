from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.auth.oidc import OIDCJWTValidator, OIDCUnavailableError, TokenValidationError


async def test_valid_token(make_token: Any, oidc_settings: Any, oidc_transport: Any) -> None:
    async with httpx.AsyncClient(transport=oidc_transport) as client:
        claims = await OIDCJWTValidator(oidc_settings, client).validate(make_token())
    assert claims["sub"] == "subject-1"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"exp": int(time.time()) - 10}, "expired"),
        ({"iss": "https://attacker.example"}, "issuer"),
        ({"aud": "wrong-audience"}, "audience"),
        ({"nbf": int(time.time()) + 300}, "not active"),
        ({"scope": "openid"}, "required scopes"),
    ],
)
async def test_invalid_registered_claims(
    make_token: Any,
    oidc_settings: Any,
    oidc_transport: Any,
    overrides: dict[str, Any],
    message: str,
) -> None:
    async with httpx.AsyncClient(transport=oidc_transport) as client:
        validator = OIDCJWTValidator(oidc_settings, client)
        with pytest.raises(TokenValidationError, match=message):
            await validator.validate(make_token(**overrides))


async def test_invalid_signature(make_token: Any, oidc_settings: Any, oidc_transport: Any) -> None:
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(make_token(), options={"verify_signature": False})
    forged = jwt.encode(claims, attacker, algorithm="RS256", headers={"kid": "test-key"})
    async with httpx.AsyncClient(transport=oidc_transport) as client:
        with pytest.raises(TokenValidationError, match="signature"):
            await OIDCJWTValidator(oidc_settings, client).validate(forged)


async def test_missing_required_subject_claim(
    make_token: Any,
    rsa_material: dict[str, Any],
    oidc_settings: Any,
    oidc_transport: Any,
) -> None:
    claims = jwt.decode(make_token(), options={"verify_signature": False})
    claims.pop("sub")
    incomplete = jwt.encode(
        claims,
        rsa_material["private"],
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    async with httpx.AsyncClient(transport=oidc_transport) as client:
        with pytest.raises(TokenValidationError, match="claims"):
            await OIDCJWTValidator(oidc_settings, client).validate(incomplete)


async def test_discovery_issuer_must_match(oidc_settings: Any, make_token: Any) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"issuer": "https://attacker.example", "jwks_uri": "https://attacker.example/jwks"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(OIDCUnavailableError, match="discovery issuer"):
            await OIDCJWTValidator(oidc_settings, client).validate(make_token())


async def test_oidc_network_failure_is_unavailable(oidc_settings: Any, make_token: Any) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(OIDCUnavailableError, match="request failed"):
            await OIDCJWTValidator(oidc_settings, client).validate(make_token())


async def test_unknown_key_refresh_is_rate_limited(
    make_token: Any,
    rsa_material: dict[str, Any],
    oidc_settings: Any,
    oidc_transport: Any,
) -> None:
    calls = 0

    def counted(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return oidc_transport.handle_request(request)

    claims = jwt.decode(make_token(), options={"verify_signature": False})
    unknown_key = jwt.encode(
        claims,
        rsa_material["private"],
        algorithm="RS256",
        headers={"kid": "attacker-controlled-key-id"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(counted)) as client:
        validator = OIDCJWTValidator(oidc_settings, client)
        for _ in range(2):
            with pytest.raises(TokenValidationError, match="unknown"):
                await validator.validate(unknown_key)

    # The first attempt does an initial fill and one rotation refresh. Further
    # unrecognized key IDs during the cooldown never trigger IdP traffic.
    assert calls == 4
