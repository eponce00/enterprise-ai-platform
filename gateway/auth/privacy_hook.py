"""Server-side OpenRouter request policy injection.

Caller-supplied provider routing can narrow the policy but cannot remove ZDR,
data-collection denial, or the organization's provider restrictions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, status

try:  # pragma: no cover - loaded inside the LiteLLM image
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:

    class CustomLogger:  # type: ignore[no-redef]
        pass


_CLIENT_ROUTING_FIELDS = frozenset(
    {
        "api_base",
        "api_key",
        "azure",
        "base_url",
        "completion_model",
        "configurable_clientside_auth_params",
        "content_policy_fallbacks",
        "context_window_fallbacks",
        "custom_llm_provider",
        "default_fallbacks",
        "deployment_id",
        "fallbacks",
        "model_group",
        "model_list",
        "models",
        "route",
        "routing_strategy",
        "specific_deployment",
        "user_config",
    }
)
_PROVIDER_PAYLOAD_ROUTING_FIELDS = _CLIENT_ROUTING_FIELDS | {"model", "router_settings_override"}
_ALLOWED_PROVIDER_FIELDS = frozenset({"data_collection", "ignore", "only", "require_parameters", "zdr"})


class EnterprisePolicyHook(CustomLogger):  # type: ignore[misc]
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: Any,
    ) -> dict[str, Any]:
        # LiteLLM can project key/team router settings into request data after
        # custom authentication. Any non-empty override would bypass the
        # startup-validated, source-controlled fallback graph.
        if data.get("router_settings_override") not in (None, {}):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="database router overrides are not allowed",
            )
        if getattr(user_api_key_dict, "aliases", None) or getattr(user_api_key_dict, "team_model_aliases", None):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="database model aliases are not allowed",
            )

        if _CLIENT_ROUTING_FIELDS.intersection(data) or _unsafe_extra_body(data.get("extra_body")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="client-controlled provider routing is not allowed",
            )

        metadata = getattr(user_api_key_dict, "metadata", {}) or {}
        if "authorized_model" in metadata and data.get("model") != metadata["authorized_model"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="model routing changed after authorization",
            )
        policy = metadata.get("openrouter_policy", {})
        extra_body = data.get("extra_body")
        nested_provider: Any = None
        has_nested_provider = False
        if isinstance(extra_body, dict):
            extra_body = dict(extra_body)
            has_nested_provider = "provider" in extra_body
            nested_provider = extra_body.pop("provider", None)
            data["extra_body"] = extra_body
        caller = data.get("provider")
        has_caller_provider = "provider" in data
        has_provider_controls = (has_nested_provider and nested_provider not in (None, {})) or (
            has_caller_provider and caller not in (None, {})
        )
        if metadata.get("route_backend") != "openrouter":
            if has_provider_controls:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="provider controls are only allowed for OpenRouter routes",
                )
            return data
        if not isinstance(policy, dict):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OpenRouter privacy policy is unavailable",
            )

        requested_provider = _validate_provider_controls(nested_provider, present=has_nested_provider)
        requested_provider.update(_validate_provider_controls(caller, present=has_caller_provider))
        # Reconstruct the provider object from reviewed narrowing controls.
        # Forwarding arbitrary provider keys would silently opt into new
        # OpenRouter routing behavior when that API evolves.
        provider: dict[str, Any] = {}
        if requested_provider.get("zdr") is True:
            provider["zdr"] = True
        if requested_provider.get("data_collection") == "deny":
            provider["data_collection"] = "deny"
        if requested_provider.get("require_parameters") is True:
            provider["require_parameters"] = True
        if policy.get("zdr", policy.get("require_zdr", False)):
            provider["zdr"] = True
        if policy.get("data_collection") == "deny" or policy.get("deny_data_collection"):
            provider["data_collection"] = "deny"

        allowed = _strings(policy.get("provider_allowlist"))
        denied = _strings(policy.get("provider_denylist"))
        caller_only = _strings(requested_provider.get("only"))
        caller_ignore = _strings(requested_provider.get("ignore"))
        if allowed:
            effective_only = set(allowed).intersection(caller_only) if caller_only else set(allowed)
            effective_only.difference_update(denied)
            if not effective_only:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="request provider constraints conflict with the privacy profile",
                )
            provider["only"] = sorted(effective_only)
        elif caller_only:
            effective_only = set(caller_only).difference(denied)
            if not effective_only:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="request provider constraints conflict with the privacy profile",
                )
            provider["only"] = sorted(effective_only)
        if denied or caller_ignore:
            provider["ignore"] = sorted(set(denied).union(caller_ignore))
        if data.get("tools") or policy.get("require_parameters"):
            provider["require_parameters"] = True
        data["provider"] = provider
        return data


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _validate_provider_controls(value: object, *, present: bool) -> dict[str, Any]:
    if not present or value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="provider controls must be a mapping",
        )
    unsupported = set(value).difference(_ALLOWED_PROVIDER_FIELDS)
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="unreviewed provider controls are not allowed",
        )
    for field in ("zdr", "require_parameters"):
        if field in value and not isinstance(value[field], bool):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="provider controls have invalid types",
            )
    if "data_collection" in value and (
        not isinstance(value["data_collection"], str) or value["data_collection"] not in {"allow", "deny"}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="provider controls have invalid types",
        )
    for field in ("only", "ignore"):
        candidate = value.get(field)
        if candidate is not None and not (
            isinstance(candidate, str)
            or (isinstance(candidate, list) and all(isinstance(item, str) for item in candidate))
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="provider controls have invalid types",
            )
    return dict(value)


def _contains_provider_routing_control(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _PROVIDER_PAYLOAD_ROUTING_FIELDS or _contains_provider_routing_control(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_provider_routing_control(item) for item in value)
    return False


def _unsafe_extra_body(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, Mapping):
        return True
    return _contains_provider_routing_control(value)


enterprise_policy_hook = EnterprisePolicyHook()
