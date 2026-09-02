"""Standards-based local validation of OIDC access tokens."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from .settings import OIDCSettings


class TokenValidationError(ValueError):
    """A bearer token failed authentication."""


class OIDCUnavailableError(TokenValidationError):
    """OIDC discovery or key material could not be obtained safely."""


class OIDCJWTValidator:
    """Validate JWTs with cached OIDC discovery and JWKS documents.

    The cache is refreshed on expiry and once immediately for an unknown key ID,
    which accommodates normal signing-key rotation without introspecting the IdP
    for every inference request.
    """

    def __init__(self, settings: OIDCSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._metadata: dict[str, Any] | None = None
        self._keys: dict[str, PyJWK] = {}
        self._cache_deadline = 0.0
        self._unknown_kid_refresh_deadline = 0.0
        self._lock = asyncio.Lock()

    async def validate(self, token: str) -> dict[str, Any]:
        if not token or token.count(".") != 2:
            raise TokenValidationError("bearer token is not a JWT")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenValidationError("invalid JWT header") from exc

        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in self.settings.algorithms:
            raise TokenValidationError("token signing algorithm is not allowed")
        if not isinstance(key_id, str) or not key_id:
            raise TokenValidationError("token has no signing key ID")

        key = await self._get_key(key_id)
        if key is None:
            key = await self._get_key(key_id, force=True)
        if key is None:
            raise TokenValidationError("token signing key is unknown")

        audience: str | list[str]
        if len(self.settings.audience) == 1:
            audience = self.settings.audience[0]
        else:
            audience = list(self.settings.audience)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(self.settings.algorithms),
                audience=audience,
                issuer=self.settings.issuer,
                leeway=self.settings.clock_skew_seconds,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("token has expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenValidationError("token is not active yet") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenValidationError("token issuer is invalid") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenValidationError("token audience is invalid") from exc
        except jwt.PyJWTError as exc:
            raise TokenValidationError("token signature or claims are invalid") from exc

        scopes = _token_scopes(claims)
        missing = set(self.settings.required_scopes).difference(scopes)
        if missing:
            raise TokenValidationError(f"token is missing required scopes: {', '.join(sorted(missing))}")

        client_id = claims.get("azp") or claims.get("client_id")
        if self.settings.allowed_client_ids and client_id not in self.settings.allowed_client_ids:
            raise TokenValidationError("token client is not allowed")
        return dict(claims)

    async def _get_key(self, key_id: str, force: bool = False) -> PyJWK | None:
        if not force and time.monotonic() < self._cache_deadline and self._keys:
            return self._keys.get(key_id)
        async with self._lock:
            if not force and time.monotonic() < self._cache_deadline and self._keys:
                return self._keys.get(key_id)
            if force and time.monotonic() < self._unknown_kid_refresh_deadline:
                return self._keys.get(key_id)
            await self._refresh()
            if force:
                self._unknown_kid_refresh_deadline = time.monotonic() + self.settings.jwks_unknown_kid_refresh_seconds
            return self._keys.get(key_id)

    async def _refresh(self) -> None:
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        try:
            metadata_response = await client.get(self.settings.discovery_url)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            if not isinstance(metadata, Mapping):
                raise OIDCUnavailableError("OIDC discovery returned an invalid document")
            if metadata.get("issuer", "").rstrip("/") != self.settings.issuer:
                raise OIDCUnavailableError("OIDC discovery issuer does not match configuration")
            jwks_uri = self.settings.jwks_url or metadata.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith(("https://", "http://")):
                raise OIDCUnavailableError("OIDC discovery has no valid jwks_uri")
            if jwks_uri.startswith("http://") and not self.settings.allow_http:
                raise OIDCUnavailableError("OIDC jwks_uri must use HTTPS")
            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
            if not isinstance(jwks, Mapping):
                raise OIDCUnavailableError("OIDC JWKS returned an invalid document")
            raw_keys = jwks.get("keys", [])
            keys: dict[str, PyJWK] = {}
            for item in raw_keys:
                if not isinstance(item, Mapping) or not isinstance(item.get("kid"), str):
                    continue
                try:
                    parsed = PyJWK.from_dict(dict(item))
                except (jwt.PyJWTError, ValueError):
                    continue
                if parsed.algorithm_name in self.settings.algorithms:
                    keys[str(item["kid"])] = parsed
            if not keys:
                raise OIDCUnavailableError("OIDC JWKS contains no allowed signing keys")
            self._metadata = dict(metadata)
            self._keys = keys
            self._cache_deadline = time.monotonic() + self.settings.jwks_ttl_seconds
        except OIDCUnavailableError:
            raise
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise OIDCUnavailableError("OIDC discovery or JWKS request failed") from exc
        finally:
            if close_client:
                await client.aclose()


def _token_scopes(claims: Mapping[str, Any]) -> set[str]:
    scopes: set[str] = set()
    for field in ("scope", "scp"):
        value = claims.get(field)
        if isinstance(value, str):
            scopes.update(value.split())
        elif isinstance(value, list):
            scopes.update(str(item) for item in value)
    return scopes
