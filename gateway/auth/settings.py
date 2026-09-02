"""Environment-backed settings shared by the authentication extension."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(ValueError):
    """Raised when security-sensitive configuration is invalid."""


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").replace(",", " ").split() if item.strip())


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean")


def _int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < 0:
        raise SettingsError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    audience: tuple[str, ...]
    discovery_url: str
    jwks_url: str | None
    algorithms: tuple[str, ...]
    required_scopes: tuple[str, ...]
    allowed_client_ids: tuple[str, ...]
    jwks_ttl_seconds: int = 3600
    jwks_unknown_kid_refresh_seconds: int = 30
    clock_skew_seconds: int = 30
    allow_http: bool = False

    @classmethod
    def from_env(cls) -> OIDCSettings:
        issuer = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
        audience = _split(os.getenv("OIDC_AUDIENCE"))
        if not issuer:
            raise SettingsError("OIDC_ISSUER_URL is required")
        if not audience:
            raise SettingsError("OIDC_AUDIENCE is required")
        allow_http = _bool("OIDC_ALLOW_HTTP")
        if issuer.startswith("http://") and not allow_http:
            raise SettingsError("OIDC_ISSUER_URL must use HTTPS unless OIDC_ALLOW_HTTP=true")
        if not issuer.startswith(("https://", "http://")):
            raise SettingsError("OIDC_ISSUER_URL must be an absolute HTTP(S) URL")

        discovery = os.getenv("OIDC_DISCOVERY_URL") or f"{issuer}/.well-known/openid-configuration"
        if discovery.startswith("http://") and not allow_http:
            raise SettingsError("OIDC_DISCOVERY_URL must use HTTPS unless OIDC_ALLOW_HTTP=true")

        algorithms = _split(os.getenv("OIDC_ALLOWED_ALGORITHMS", "RS256"))
        insecure = {"none", "HS256", "HS384", "HS512"}.intersection(algorithms)
        if insecure:
            raise SettingsError("only asymmetric OIDC signing algorithms are allowed")

        return cls(
            issuer=issuer,
            audience=audience,
            discovery_url=discovery,
            jwks_url=os.getenv("OIDC_JWKS_URL") or None,
            algorithms=algorithms,
            required_scopes=_split(os.getenv("OIDC_REQUIRED_SCOPES", "")),
            allowed_client_ids=_split(os.getenv("OIDC_ALLOWED_CLIENT_IDS") or os.getenv("OIDC_CLIENT_ID")),
            jwks_ttl_seconds=_int("OIDC_JWKS_TTL_SECONDS", 3600),
            jwks_unknown_kid_refresh_seconds=_int("OIDC_JWKS_UNKNOWN_KID_REFRESH_SECONDS", 30),
            clock_skew_seconds=_int("OIDC_CLOCK_SKEW_SECONDS", 30),
            allow_http=allow_http,
        )


@dataclass(frozen=True)
class GatewaySettings:
    policy_file: Path
    master_key: str | None

    @classmethod
    def from_env(cls) -> GatewaySettings:
        return cls(
            policy_file=Path(os.getenv("OIDC_POLICY_FILE", "/app/policy.yaml")),
            master_key=os.getenv("LITELLM_MASTER_KEY"),
        )
