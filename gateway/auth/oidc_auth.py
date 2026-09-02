"""LiteLLM OSS custom-auth entry point.

Configured as `general_settings.custom_auth` in `gateway/litellm/config.yaml`.
No virtual key is minted: the validated OIDC identity is returned directly as a
LiteLLM UserAPIKeyAuth object.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, Request, status

from .claims import extract_identity
from .litellm_compat import LitellmUserRoles, UserAPIKeyAuth
from .oidc import OIDCJWTValidator, OIDCUnavailableError, TokenValidationError
from .policy import PolicyDecision, PolicyDenied, PolicyEngine, PolicyError
from .settings import GatewaySettings, OIDCSettings, SettingsError

logger = logging.getLogger(__name__)

_validator: OIDCJWTValidator | None = None
_policy: PolicyEngine | None = None
_runtime_models: tuple[str, ...] | None = None

_DEFAULT_RUNTIME_CONFIG = "/tmp/enterprise-ai-litellm.yaml"  # noqa: S108 - shared non-sensitive runtime config


class _RuntimeModelConfigError(RuntimeError):
    """The rendered LiteLLM config cannot provide a safe model inventory."""


def _load() -> tuple[OIDCJWTValidator, PolicyEngine, GatewaySettings]:
    global _validator, _policy
    gateway = GatewaySettings.from_env()
    if _validator is None:
        _validator = OIDCJWTValidator(OIDCSettings.from_env())
    if _policy is None:
        _policy = PolicyEngine.from_file(gateway.policy_file)
    return _validator, _policy, gateway


def _load_runtime_models() -> tuple[str, ...]:
    """Load exact model names from the rendered config used by LiteLLM."""

    global _runtime_models
    if _runtime_models is not None:
        return _runtime_models

    path = Path(os.getenv("LITELLM_RUNTIME_CONFIG", _DEFAULT_RUNTIME_CONFIG))
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _RuntimeModelConfigError(f"cannot load LiteLLM runtime config: {path}") from exc

    if not isinstance(document, Mapping):
        raise _RuntimeModelConfigError("LiteLLM runtime config must be a mapping")
    model_list = document.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise _RuntimeModelConfigError("LiteLLM runtime config must contain a non-empty model_list")

    models: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(model_list):
        if not isinstance(entry, Mapping):
            raise _RuntimeModelConfigError(f"model_list entry {index} must be a mapping")
        model_name = entry.get("model_name")
        if not isinstance(model_name, str) or not model_name or model_name != model_name.strip():
            raise _RuntimeModelConfigError(f"model_list entry {index} has an invalid model_name")
        if any(character in model_name for character in "*?[]"):
            raise _RuntimeModelConfigError(f"model_list entry {index} has a non-exact model_name")
        # LiteLLM permits multiple deployments for one public model name.
        if model_name not in seen:
            seen.add(model_name)
            models.append(model_name)

    _runtime_models = tuple(models)
    return _runtime_models


def _authorized_runtime_models(
    decision: PolicyDecision,
    runtime_models: tuple[str, ...],
) -> tuple[str, ...]:
    """Expand policy patterns against the exact configured runtime inventory."""

    return tuple(
        model
        for model in runtime_models
        if any(fnmatch.fnmatchcase(model, pattern) for pattern in decision.allowed_models)
        and not any(fnmatch.fnmatchcase(model, pattern) for pattern in decision.denied_models)
    )


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    """Authenticate a LiteLLM request with a locally validated OIDC JWT."""

    try:
        validator, policy, gateway = _load()
    except (SettingsError, PolicyError) as exc:
        logger.exception("gateway authentication configuration is invalid")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication unavailable",
        ) from exc

    token = _bearer_value(request, api_key)
    # Permit LiteLLM's own server-side management calls. This exact secret is
    # never delivered to a human client and compare_digest avoids timing leaks.
    if gateway.master_key and hmac.compare_digest(token, gateway.master_key):
        return UserAPIKeyAuth(
            api_key=token,
            token=_stable_token("litellm-admin"),
            user_id="litellm-proxy-admin",
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )

    try:
        claims = await validator.validate(token)
        identity = extract_identity(
            claims,
            group_claim=os.getenv("OIDC_GROUP_CLAIM") or policy.group_claim,
            role_claim=os.getenv("OIDC_ROLE_CLAIM") or policy.role_claim,
            service_claim=os.getenv("OIDC_SERVICE_CLAIM") or policy.service_claim,
            service_values=policy.service_values,
        )
        decision = policy.resolve(identity)
        body = await _request_json(request)
        model = str(body.get("model") or "")
        runtime_models = _load_runtime_models()
        if model and model not in runtime_models:
            raise PolicyDenied(f"model {model!r} is not configured by the gateway runtime")
        if model:
            policy.authorize_model(decision, model)
        authorized_models = _authorized_runtime_models(decision, runtime_models)
        if not authorized_models:
            raise PolicyDenied("team has no authorized models in the gateway runtime")
    except _RuntimeModelConfigError as exc:
        logger.exception("LiteLLM runtime model configuration is invalid")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication unavailable",
        ) from exc
    except OIDCUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication unavailable",
        ) from exc
    except TokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except PolicyDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except (ValueError, PolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="identity mapping failed") from exc

    client_header = request.headers.get("x-enterprise-ai-client", "")
    untrusted_client = _safe_telemetry_value(client_header)
    trusted_application = identity.client_id or "unknown"
    metadata: dict[str, Any] = {
        "application": trusted_application,
        "client_reported_application": untrusted_client,
        "identity_kind": identity.kind,
        "organization": decision.organization,
        "privacy_profile": decision.profile,
        "openrouter_policy": dict(decision.privacy),
        "oidc_subject": identity.subject,
    }

    # The stable synthetic token keys LiteLLM rate/spend accounting without
    # retaining the bearer token itself. DB team/user rows, when bootstrapped or
    # reconciled out of band, supply current spend to common checks.
    return UserAPIKeyAuth(
        token=_stable_token(identity.stable_id),
        # LiteLLM persists key_alias in OSS spend-log metadata. Encoding the
        # trusted identity kind/client claim here keeps application attribution
        # durable without relying on Enterprise custom spend-log metadata.
        key_alias=_safe_telemetry_value(f"{identity.kind}/{trusted_application}"),
        expires=_iso_expiry(claims.get("exp")),
        user_id=identity.stable_id,
        user_email=identity.email,
        user_role=LitellmUserRoles.INTERNAL_USER,
        team_id=decision.team,
        team_alias=decision.team,
        models=list(authorized_models),
        # Do not copy the team grant or budget onto the token. LiteLLM treats a
        # non-empty team_models value as a snapshot that may vouch for a team
        # when its Prisma lookup fails. Team policy must come from the DB row so
        # an unreadable row denies instead of resetting accumulated spend.
        team_models=[],
        rpm_limit=decision.rpm_limit,
        user_rpm_limit=decision.rpm_limit,
        tpm_limit=decision.tpm_limit,
        user_tpm_limit=decision.tpm_limit,
        metadata=metadata,
        jwt_claims={key: claims[key] for key in ("iss", "sub", "azp") if key in claims},
    )


def _bearer_value(request: Request, api_key: str) -> str:
    header = request.headers.get("authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer authentication is required")
        return value.strip()
    value = api_key.removeprefix("Bearer ").strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer authentication is required")
    return value


async def _request_json(request: Request) -> dict[str, Any]:
    if request.method.upper() not in {"POST", "PUT", "PATCH"}:
        return {}
    try:
        value = await request.json()
    except Exception:  # LiteLLM supports non-JSON endpoints; auth remains valid.
        return {}
    return value if isinstance(value, dict) else {}


def _stable_token(identity: str) -> str:
    return "oidc-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _iso_expiry(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_telemetry_value(value: str) -> str:
    return "".join(char for char in value[:80] if char.isalnum() or char in "._-/")


def reset_runtime_caches() -> None:
    """Test helper; production processes build each cache once."""
    global _validator, _policy, _runtime_models
    _validator = None
    _policy = None
    _runtime_models = None
