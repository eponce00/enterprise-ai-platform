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
from urllib.parse import urlsplit

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
_runtime_model_backends: dict[str, str] | None = None
_runtime_model_fallbacks: dict[str, tuple[str, ...]] | None = None

_DEFAULT_RUNTIME_CONFIG = "/tmp/enterprise-ai-litellm.yaml"  # noqa: S108 - shared non-sensitive runtime config
_FALLBACK_FIELDS = ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks")
_OIDC_DATA_PLANE_ROUTES = frozenset({("GET", "/v1/models"), ("POST", "/v1/chat/completions")})
_MASTER_KEY_MANAGEMENT_ROUTES = frozenset(
    {
        ("GET", "/spend/logs/v2"),
        ("GET", "/team/info"),
        ("POST", "/team/new"),
        ("POST", "/team/update"),
    }
)
_UNSUPPORTED_DEPLOYMENT_ROUTING_FIELDS = frozenset(
    (
        *_FALLBACK_FIELDS,
        "azure",
        "completion_model",
        "default_fallbacks",
        "deployment_id",
        "model_group_alias",
        "model_list",
        "router_settings_override",
        "silent_model",
        "specific_deployment",
        "user_config",
    )
)
_CLIENT_ROUTING_FIELDS = frozenset(
    (
        *_FALLBACK_FIELDS,
        "default_fallbacks",
        "router_settings_override",
        "api_base",
        "base_url",
        "api_key",
        "user_config",
        "custom_llm_provider",
        "specific_deployment",
        "deployment_id",
        "model_group",
        "model_list",
        "completion_model",
        "azure",
    )
)


class _RuntimeModelConfigError(RuntimeError):
    """The rendered LiteLLM config cannot provide a safe model inventory."""


class RuntimeModelPolicyError(RuntimeError):
    """The rendered route inventory cannot preserve provider policy."""


def validate_runtime_model_policy() -> None:
    """Fail startup before serving when the rendered route policy is unsafe."""

    try:
        _load_runtime_models()
    except _RuntimeModelConfigError as exc:
        raise RuntimeModelPolicyError(str(exc)) from exc


def _load() -> tuple[OIDCJWTValidator, PolicyEngine, GatewaySettings]:
    global _validator, _policy
    gateway = GatewaySettings.from_env()
    if _validator is None:
        _validator = OIDCJWTValidator(OIDCSettings.from_env())
    if _policy is None:
        _policy = PolicyEngine.from_file(gateway.policy_file)
    return _validator, _policy, gateway


def _load_runtime_models() -> tuple[tuple[str, ...], dict[str, str], dict[str, tuple[str, ...]]]:
    """Load exact model names and deployment-derived backend classifications."""

    global _runtime_model_backends, _runtime_model_fallbacks, _runtime_models
    if _runtime_models is not None and _runtime_model_backends is not None and _runtime_model_fallbacks is not None:
        return _runtime_models, _runtime_model_backends, _runtime_model_fallbacks

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
    backends: dict[str, str] = {}
    seen: set[str] = set()
    for index, entry in enumerate(model_list):
        if not isinstance(entry, Mapping):
            raise _RuntimeModelConfigError(f"model_list entry {index} must be a mapping")
        model_name = entry.get("model_name")
        if not isinstance(model_name, str) or not model_name or model_name != model_name.strip():
            raise _RuntimeModelConfigError(f"model_list entry {index} has an invalid model_name")
        if any(character in model_name for character in "*?[]"):
            raise _RuntimeModelConfigError(f"model_list entry {index} has a non-exact model_name")
        params = entry.get("litellm_params")
        if not isinstance(params, Mapping):
            raise _RuntimeModelConfigError(f"model_list entry {index} has invalid litellm_params")
        target = _resolve_runtime_value(params.get("model"), index=index, field="model")
        deployment_routing_fields = _UNSUPPORTED_DEPLOYMENT_ROUTING_FIELDS.intersection(params)
        if deployment_routing_fields:
            raise _RuntimeModelConfigError(
                f"model_list entry {index} uses unsupported deployment-level routing controls"
            )
        provider_value = params.get("custom_llm_provider")
        provider = (
            _resolve_runtime_value(provider_value, index=index, field="custom_llm_provider").lower()
            if provider_value is not None
            else None
        )
        target_is_openrouter = target.startswith("openrouter/")
        if target_is_openrouter and provider not in {None, "openrouter"}:
            raise _RuntimeModelConfigError(
                f"model_list entry {index} has contradictory model and custom_llm_provider values"
            )
        backend = "openrouter" if target_is_openrouter or provider == "openrouter" else "direct"
        if "api_base" in params and "base_url" in params:
            raise _RuntimeModelConfigError(f"model_list entry {index} has ambiguous API base configuration")
        for api_base_field in ("api_base", "base_url"):
            api_base_value = params.get(api_base_field)
            if api_base_value is None:
                continue
            api_base = _resolve_runtime_value(api_base_value, index=index, field=api_base_field)
            try:
                parsed_api_base = urlsplit(api_base)
                api_host = (parsed_api_base.hostname or "").lower().rstrip(".")
                _ = parsed_api_base.port
            except ValueError as exc:
                raise _RuntimeModelConfigError(f"model_list entry {index} has invalid {api_base_field}") from exc
            if (
                parsed_api_base.scheme not in {"http", "https"}
                or not api_host
                or parsed_api_base.username is not None
                or parsed_api_base.password is not None
            ):
                raise _RuntimeModelConfigError(f"model_list entry {index} has invalid {api_base_field}")
            is_openrouter_host = api_host == "openrouter.ai" or api_host.endswith(".openrouter.ai")
            if is_openrouter_host and backend != "openrouter":
                raise _RuntimeModelConfigError(
                    f"model_list entry {index} uses the OpenRouter API without the OpenRouter model adapter"
                )
        prior_backend = backends.get(model_name)
        if prior_backend is not None and prior_backend != backend:
            raise _RuntimeModelConfigError(f"model {model_name!r} mixes OpenRouter and direct deployments")
        backends[model_name] = backend
        # LiteLLM permits multiple deployments for one public model name.
        if model_name not in seen:
            seen.add(model_name)
            models.append(model_name)

    fallbacks = _validate_runtime_fallbacks(document, backends, models)
    _runtime_models = tuple(models)
    _runtime_model_backends = backends
    _runtime_model_fallbacks = fallbacks
    return _runtime_models, _runtime_model_backends, _runtime_model_fallbacks


def _resolve_runtime_value(value: object, *, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _RuntimeModelConfigError(f"model_list entry {index} has invalid {field}")
    value = value.strip()
    prefix = "os.environ/"
    if not value.startswith(prefix):
        return value
    variable = value.removeprefix(prefix)
    resolved = os.getenv(variable, "").strip()
    if not variable or not resolved:
        raise _RuntimeModelConfigError(f"model_list entry {index} references unavailable {field} configuration")
    return resolved


def _validate_runtime_fallbacks(
    document: Mapping[str, Any],
    backends: Mapping[str, str],
    models: list[str],
) -> dict[str, tuple[str, ...]]:
    litellm_settings = document.get("litellm_settings")
    if isinstance(litellm_settings, Mapping) and any(
        field in litellm_settings for field in (*_FALLBACK_FIELDS, "default_fallbacks")
    ):
        raise _RuntimeModelConfigError("fallback routing must be configured under LiteLLM router_settings")

    router = document.get("router_settings")
    if router is None:
        return {}
    if not isinstance(router, Mapping):
        raise _RuntimeModelConfigError("LiteLLM router_settings must be a mapping")
    if "model_group_alias" in router:
        raise _RuntimeModelConfigError("LiteLLM model_group_alias routing is not supported by this policy layer")
    graph: dict[str, set[str]] = {}
    for field in _FALLBACK_FIELDS:
        fallbacks = router.get(field)
        if fallbacks is None:
            continue
        if not isinstance(fallbacks, list):
            raise _RuntimeModelConfigError(f"LiteLLM router {field} must be a list")
        for index, entry in enumerate(fallbacks):
            if not isinstance(entry, Mapping) or len(entry) != 1:
                raise _RuntimeModelConfigError(f"router {field} entry {index} must map one model to a list")
            source, targets = next(iter(entry.items()))
            if not isinstance(source, str) or source not in backends:
                raise _RuntimeModelConfigError(f"router {field} entry {index} references an unknown source model")
            if not isinstance(targets, list) or not targets:
                raise _RuntimeModelConfigError(f"router {field} entry {index} must contain target models")
            for target in targets:
                _validate_fallback_target(field, index, source, target, backends)
                graph.setdefault(source, set()).add(target)

    default_fallbacks = router.get("default_fallbacks")
    if default_fallbacks is not None:
        raise _RuntimeModelConfigError("LiteLLM router default_fallbacks is not supported by this policy layer")

    _validate_acyclic_fallbacks(graph)

    closures: dict[str, tuple[str, ...]] = {}
    for source in models:
        reachable: set[str] = set()
        pending = list(graph.get(source, ()))
        while pending:
            target = pending.pop()
            if target in reachable:
                continue
            reachable.add(target)
            pending.extend(graph.get(target, ()))
        reachable.discard(source)
        if reachable:
            closures[source] = tuple(model for model in models if model in reachable)
    return closures


def _validate_acyclic_fallbacks(graph: Mapping[str, set[str]]) -> None:
    nodes = set(graph)
    nodes.update(target for targets in graph.values() for target in targets)
    indegree = dict.fromkeys(nodes, 0)
    for source, targets in graph.items():
        if source in targets:
            raise _RuntimeModelConfigError(f"router fallback graph contains a self-cycle for {source!r}")
        for target in targets:
            indegree[target] += 1
    pending = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while pending:
        source = pending.pop()
        visited += 1
        for target in graph.get(source, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(nodes):
        raise _RuntimeModelConfigError("router fallback graph contains a cycle")


def _validate_fallback_target(
    field: str,
    index: int,
    source: str,
    target: object,
    backends: Mapping[str, str],
) -> None:
    if not isinstance(target, str) or target not in backends:
        raise _RuntimeModelConfigError(f"router {field} entry {index} references an unknown target model")
    if backends[source] != backends[target]:
        raise _RuntimeModelConfigError(
            f"router {field} {source!r} to {target!r} mixes OpenRouter and direct deployments"
        )


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
    # Permit LiteLLM's server-side management calls only. The exact secret is
    # never delivered to a human client, compare_digest avoids timing leaks,
    # and the returned audit identifier does not retain the secret.
    if gateway.master_key and hmac.compare_digest(token, gateway.master_key):
        management_route = (request.method.upper(), _request_path(request))
        is_spend_detail = management_route[0] == "GET" and management_route[1].startswith("/spend/logs/ui/")
        if management_route not in _MASTER_KEY_MANAGEMENT_ROUTES and not is_spend_detail:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="the management credential is not allowed on this route",
            )
        return UserAPIKeyAuth(
            api_key="litellm_proxy_master_key",
            token=_stable_token("litellm-admin"),
            user_id="litellm-proxy-admin",
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )

    if (request.method.upper(), _request_path(request)) not in _OIDC_DATA_PLANE_ROUTES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OIDC credentials are not allowed on this route",
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
        if _CLIENT_ROUTING_FIELDS.intersection(body):
            raise PolicyDenied("client-controlled provider routing is not allowed")
        model = str(body.get("model") or "")
        runtime_models, runtime_backends, runtime_fallbacks = _load_runtime_models()
        if model and model not in runtime_models:
            raise PolicyDenied(f"model {model!r} is not configured by the gateway runtime")
        if model:
            policy.authorize_model(decision, model)
            for fallback_model in runtime_fallbacks.get(model, ()):
                try:
                    policy.authorize_model(decision, fallback_model)
                except PolicyDenied as exc:
                    raise PolicyDenied(
                        f"configured fallback {fallback_model!r} is not authorized for this identity"
                    ) from exc
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
        # The pre-call hook compares this trusted value with LiteLLM's final
        # pre-routing model name, catching DB/global alias rewrites that occur
        # after custom authentication.
        "authorized_model": model or None,
        "route_backend": runtime_backends.get(model) if model else None,
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
        # when its Prisma lookup fails. The DB row therefore remains required
        # for model/budget authorization and an unreadable row denies instead
        # of resetting accumulated spend.
        team_models=[],
        # LiteLLM 1.99.0 common checks do not project a DB team's RPM/TPM fields
        # onto a centralized custom-auth object. Carry the same source-controlled
        # values reconciled by bootstrap so its v3 limiter enforces both the
        # aggregate team and stable per-identity dimensions.
        team_rpm_limit=decision.rpm_limit,
        user_rpm_limit=decision.rpm_limit,
        team_tpm_limit=decision.tpm_limit,
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


def _request_path(request: Request) -> str:
    """Use the ASGI dispatch path, never a Host-derived reconstructed URL."""

    path = request.scope.get("path")
    return path if isinstance(path, str) else ""


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
    global _runtime_model_backends, _runtime_model_fallbacks, _runtime_models, _validator, _policy
    _validator = None
    _policy = None
    _runtime_models = None
    _runtime_model_backends = None
    _runtime_model_fallbacks = None
