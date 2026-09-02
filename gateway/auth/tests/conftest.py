from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from gateway.auth.settings import OIDCSettings


@pytest.fixture
def rsa_material() -> dict[str, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return {"private": private_key, "jwk": public_jwk}


@pytest.fixture
def oidc_settings() -> OIDCSettings:
    return OIDCSettings(
        issuer="https://identity.example.com/realms/test",
        audience=("enterprise-ai-gateway",),
        discovery_url="https://identity.example.com/realms/test/.well-known/openid-configuration",
        jwks_url=None,
        algorithms=("RS256",),
        required_scopes=("inference",),
        allowed_client_ids=(),
        jwks_ttl_seconds=3600,
        clock_skew_seconds=0,
    )


@pytest.fixture
def make_token(rsa_material: dict[str, Any], oidc_settings: OIDCSettings) -> Callable[..., str]:
    def factory(**overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": oidc_settings.issuer,
            "sub": "subject-1",
            "aud": "enterprise-ai-gateway",
            "exp": now + 300,
            "iat": now,
            "scope": "openid inference",
            "email": "developer@example.com",
            "groups": ["ai-developers"],
            "realm_access": {"roles": ["developer"]},
            "azp": "enterprise-ai-cli",
        }
        claims.update(overrides)
        return jwt.encode(claims, rsa_material["private"], algorithm="RS256", headers={"kid": "test-key"})

    return factory


@pytest.fixture
def oidc_transport(rsa_material: dict[str, Any], oidc_settings: OIDCSettings) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": oidc_settings.issuer,
                    "jwks_uri": "https://identity.example.com/realms/test/certs",
                },
            )
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [rsa_material["jwk"]]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)
