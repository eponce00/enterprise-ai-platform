"""Server-side OpenRouter request policy injection.

Caller-supplied provider routing can narrow the policy but cannot remove ZDR,
data-collection denial, or the organization's provider restrictions.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

try:  # pragma: no cover - loaded inside the LiteLLM image
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:

    class CustomLogger:  # type: ignore[no-redef]
        pass


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

        metadata = getattr(user_api_key_dict, "metadata", {}) or {}
        if "authorized_model" in metadata and data.get("model") != metadata["authorized_model"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="model routing changed after authorization",
            )
        policy = metadata.get("openrouter_policy", {})
        if not isinstance(policy, dict) or metadata.get("route_backend") != "openrouter":
            return data

        extra_body = data.get("extra_body")
        nested_provider: Any = None
        if isinstance(extra_body, dict):
            extra_body = dict(extra_body)
            nested_provider = extra_body.pop("provider", None)
            data["extra_body"] = extra_body
        caller = data.get("provider")
        provider = dict(nested_provider) if isinstance(nested_provider, dict) else {}
        if isinstance(caller, dict):
            provider.update(caller)
        if policy.get("zdr", policy.get("require_zdr", False)):
            provider["zdr"] = True
        if policy.get("data_collection") == "deny" or policy.get("deny_data_collection"):
            provider["data_collection"] = "deny"

        allowed = _strings(policy.get("provider_allowlist"))
        denied = _strings(policy.get("provider_denylist"))
        caller_only = _strings(provider.get("only"))
        caller_ignore = _strings(provider.get("ignore"))
        if allowed:
            effective_only = set(allowed).intersection(caller_only) if caller_only else set(allowed)
            effective_only.difference_update(denied)
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


enterprise_policy_hook = EnterprisePolicyHook()
