from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException, Request
from starlette.datastructures import URL

import gateway.auth.oidc_auth as auth_module
from gateway.auth.oidc import OIDCUnavailableError
from gateway.auth.oidc_auth import (
    RuntimeModelPolicyError,
    reset_runtime_caches,
    user_api_key_auth,
    validate_runtime_model_policy,
)
from gateway.auth.policy import PolicyEngine


class StaticValidator:
    def __init__(self, claims: dict[str, Any]):
        self.claims = claims

    async def validate(self, _: str) -> dict[str, Any]:
        return self.claims


def request(
    token: str,
    body: dict[str, Any] | None = None,
    *,
    path: str = "/v1/chat/completions",
    method: str = "POST",
) -> Request:
    payload = json.dumps(body or {}).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"x-enterprise-ai-client", b"opencode"),
            ],
        },
        receive,
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reset_runtime_caches()
    monkeypatch.setenv("OIDC_POLICY_FILE", str(tmp_path / "unused.yaml"))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "admin-secret")
    runtime_config = tmp_path / "litellm-runtime.yaml"
    write_runtime_config(runtime_config, ["general-fast", "cheap-batch"])
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    yield
    reset_runtime_caches()


def write_runtime_config(path: Path, models: list[str], *, target: str = "openai/gpt-4o") -> None:
    path.write_text(
        yaml.safe_dump(
            {"model_list": [{"model_name": model, "litellm_params": {"model": target}} for model in models]}
        ),
        encoding="utf-8",
    )


def set_runtime(
    claims: dict[str, Any],
    *,
    developer_allow: list[str] | None = None,
    developer_deny: list[str] | None = None,
) -> None:
    developer_models: dict[str, list[str]] = {"allow": ["general-fast"] if developer_allow is None else developer_allow}
    if developer_deny is not None:
        developer_models["deny"] = developer_deny
    auth_module._validator = StaticValidator(claims)  # type: ignore[assignment]
    auth_module._policy = PolicyEngine(
        {
            "identity": {"organization": "organization"},
            "mappings": [
                {"client_id": "service", "kind": "service", "team": "automation"},
                {"oidc_group": "developers", "kind": "human", "team": "developers"},
            ],
            "teams": {
                "developers": {
                    "monthly_budget_usd": 20,
                    "rpm_limit": 120,
                    "tpm_limit": 1_000_000,
                    "models": developer_models,
                },
                "automation": {"models": {"allow": ["cheap-batch"]}},
            },
            "privacy_profiles": {"default": {"zdr": True, "data_collection": "deny"}},
        }
    )


def test_litellm_config_does_not_opt_into_database_auth_fallback() -> None:
    config_path = Path(__file__).parents[2] / "litellm" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["general_settings"]["custom_auth_run_common_checks"] is True
    assert config["general_settings"].get("allow_requests_on_db_unavailable", False) is False
    assert config["general_settings"]["fail_closed_budget_enforcement"] is True
    assert config["general_settings"]["user_api_key_cache_ttl"] == 5
    assert config["general_settings"]["supported_db_objects"] == []
    assert config["general_settings"]["store_prompts_in_spend_logs"] is False
    assert config["litellm_settings"]["enable_post_custom_auth_checks"] is True
    assert config["litellm_settings"]["turn_off_message_logging"] is True
    assert config["litellm_settings"]["global_disable_no_log_param"] is True


async def test_human_auth_returns_attribution_without_raw_token() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "email": "user@example.com",
            "groups": ["developers"],
            "azp": "opencode",
        }
    )
    result = await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")
    assert result.user_id == "https://idp.example|user-1"
    assert result.team_id == "developers"
    assert result.metadata["application"] == "opencode"
    assert result.metadata["organization"] == "organization"
    assert result.metadata["authorized_model"] == "general-fast"
    assert result.metadata["route_backend"] == "direct"
    assert result.key_alias == "human/opencode"
    assert "raw.jwt.token" not in result.token


async def test_database_team_policy_is_not_copied_to_fallback_token() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    result = await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    # LiteLLM only permits a token to vouch for an unreadable team when
    # team_models is non-empty. Keep the key-level defense-in-depth allowlist,
    # but require Prisma to provide team models, spend, and budget. Pinned
    # LiteLLM does not project DB rate fields after centralized custom auth, so
    # the source-controlled rates must also be carried for its v3 limiter.
    assert result.models == ["general-fast"]
    assert result.team_models == []
    assert getattr(result, "team_max_budget", None) is None
    assert result.team_rpm_limit == 120
    assert result.user_rpm_limit == 120
    assert result.team_tpm_limit == 1_000_000
    assert result.user_tpm_limit == 1_000_000


async def test_unprovisioned_organization_is_telemetry_only() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    result = await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    # A real LiteLLM org_id is populated later from a provisioned team row.
    # The policy label remains useful for telemetry without implying that an
    # organization DB budget was loaded or enforced.
    assert getattr(result, "org_id", None) is None
    assert getattr(result, "organization_alias", None) is None
    assert result.metadata["organization"] == "organization"


async def test_service_auth() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "service-1",
            "exp": 4_000_000_000,
            "azp": "service",
            "identity_type": "service",
        }
    )
    result = await user_api_key_auth(request("raw.jwt.token", {"model": "cheap-batch"}), "raw.jwt.token")
    assert result.team_id == "automation"
    assert result.metadata["identity_kind"] == "service"
    assert result.key_alias == "service/service"


async def test_unknown_group_is_forbidden() -> None:
    set_runtime({"iss": "https://idp.example", "sub": "unknown", "exp": 4_000_000_000, "groups": ["other"]})
    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")
    assert caught.value.status_code == 403


async def test_oidc_dependency_failure_is_retryable() -> None:
    set_runtime({})

    class UnavailableValidator:
        async def validate(self, _: str) -> dict[str, Any]:
            raise OIDCUnavailableError("OIDC discovery or JWKS request failed")

    auth_module._validator = UnavailableValidator()  # type: ignore[assignment]
    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")
    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_policy_patterns_expand_to_exact_runtime_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "expanded-runtime.yaml"
    write_runtime_config(
        runtime_config,
        ["coding-fast", "coding-frontier", "general-fast", "cheap-batch"],
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        },
        developer_allow=["coding-*", "general-*", "openrouter/*"],
        developer_deny=["coding-front*"],
    )

    result = await user_api_key_auth(request("raw.jwt.token"), "raw.jwt.token")

    assert result.models == ["coding-fast", "general-fast"]
    assert "coding-*" not in result.models
    assert "openrouter/*" not in result.models


async def test_policy_wildcard_cannot_authorize_unconfigured_raw_model() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        },
        developer_allow=["general-fast", "openrouter/*"],
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(
            request("raw.jwt.token", {"model": "openrouter/vendor/unapproved"}),
            "raw.jwt.token",
        )

    assert caught.value.status_code == 403
    assert "not configured by the gateway runtime" in caught.value.detail


async def test_missing_runtime_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(tmp_path / "missing.yaml"))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token"), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_runtime_backend_classification_resolves_environment_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "openrouter-runtime.yaml"
    write_runtime_config(runtime_config, ["general-fast"], target="os.environ/TEST_RUNTIME_MODEL")
    monkeypatch.setenv("TEST_RUNTIME_MODEL", "openrouter/openai/gpt-4o-mini")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    result = await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert result.metadata["route_backend"] == "openrouter"


async def test_runtime_backend_classification_honors_explicit_provider_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "hinted-openrouter-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": "general-fast",
                        "litellm_params": {
                            "model": "vendor/model",
                            "custom_llm_provider": "openrouter",
                            "api_base": "https://gateway.example/v1",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    result = await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert result.metadata["route_backend"] == "openrouter"


@pytest.mark.parametrize(
    "model,api_base",
    [
        ("os.environ/MISSING_RUNTIME_MODEL", None),
        ("openai/gpt-4o", "https://openrouter.ai/api/v1"),
        ("openai/gpt-4o", "https://openrouter.ai./api/v1"),
        ("openai/gpt-4o", "not-an-http-url"),
    ],
)
async def test_ambiguous_runtime_backend_fails_closed(
    model: str,
    api_base: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    params = {"model": model}
    if api_base is not None:
        params["api_base"] = api_base
    runtime_config = tmp_path / "ambiguous-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump({"model_list": [{"model_name": "general-fast", "litellm_params": params}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


@pytest.mark.parametrize(
    "extra_params",
    [
        {"model": "openrouter/openai/gpt-4o", "custom_llm_provider": "openai"},
        {"model": "openai/gpt-4o", "silent_model": "shadow-model"},
        {"model": "openai/gpt-4o", "fallbacks": ["unreviewed-model"]},
        {"model": "openai/gpt-4o", "context_window_fallbacks": ["unreviewed-model"]},
        {"model": "openai/gpt-4o", "content_policy_fallbacks": ["unreviewed-model"]},
        {"model": "openai/gpt-4o", "default_fallbacks": ["unreviewed-model"]},
        {"model": "openai/gpt-4o", "model_list": [{"model_name": "unreviewed-model"}]},
        {"model": "openai/gpt-4o", "deployment_id": "unreviewed-deployment"},
        {"model": "openai/gpt-4o", "azure": True},
        {
            "model": "openai/gpt-4o",
            "api_base": "https://direct.example/v1",
            "base_url": "https://direct.example/v1",
        },
        {"model": "openai/gpt-4o", "base_url": "https://openrouter.ai/api/v1"},
    ],
)
async def test_unsafe_deployment_routing_features_fail_closed(
    extra_params: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "unsafe-deployment-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump({"model_list": [{"model_name": "general-fast", "litellm_params": extra_params}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_duplicate_runtime_model_cannot_mix_backend_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "mixed-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}},
                    {
                        "model_name": "general-fast",
                        "litellm_params": {"model": "openrouter/openai/gpt-4o-mini"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_runtime_fallback_cannot_mix_backend_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "mixed-fallback-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {
                        "model_name": "general-quality",
                        "litellm_params": {"model": "openrouter/openai/gpt-4o"},
                    },
                ],
                "router_settings": {"fallbacks": [{"general-fast": ["general-quality"]}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(RuntimeModelPolicyError, match="mixes OpenRouter and direct"):
        validate_runtime_model_policy()

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


@pytest.mark.parametrize(
    "field",
    ["fallbacks", "context_window_fallbacks", "content_policy_fallbacks"],
)
async def test_every_configured_fallback_class_is_validated(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / f"mixed-{field}-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {
                        "model_name": "general-quality",
                        "litellm_params": {"model": "openrouter/openai/gpt-4o"},
                    },
                ],
                "router_settings": {field: [{"general-fast": ["general-quality"]}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_configured_default_fallback_is_rejected_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "mixed-default-fallback-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {
                        "model_name": "general-quality",
                        "litellm_params": {"model": "openrouter/openai/gpt-4o"},
                    },
                ],
                "router_settings": {"default_fallbacks": ["general-quality"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


@pytest.mark.parametrize(
    "fallbacks",
    [
        [{"general-fast": ["general-fast"]}],
        [{"general-fast": ["general-quality"]}, {"general-quality": ["general-fast"]}],
    ],
)
async def test_configured_fallback_cycles_fail_startup(
    fallbacks: list[dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "cyclic-fallback-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                ],
                "router_settings": {"fallbacks": fallbacks},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="cycle"):
        validate_runtime_model_policy()


async def test_model_group_alias_routing_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "model-group-alias-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                ],
                "router_settings": {"model_group_alias": {"general-fast": "general-quality"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="model_group_alias"):
        validate_runtime_model_policy()


async def test_configured_fallback_target_must_be_authorized_for_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "unauthorized-fallback-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                    {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                ],
                "router_settings": {"fallbacks": [{"general-fast": ["general-quality"]}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        },
        developer_allow=["general-fast"],
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token", {"model": "general-fast"}), "raw.jwt.token")

    assert caught.value.status_code == 403
    assert "configured fallback 'general-quality' is not authorized" in caught.value.detail


@pytest.mark.parametrize(
    "routing_field",
    [
        {"fallbacks": [{"general-fast": ["cheap-batch"]}]},
        {"context_window_fallbacks": [{"general-fast": ["cheap-batch"]}]},
        {"content_policy_fallbacks": [{"general-fast": ["cheap-batch"]}]},
        {"default_fallbacks": ["cheap-batch"]},
        {"router_settings_override": {"fallbacks": [{"general-fast": ["cheap-batch"]}]}},
        {"api_base": "https://attacker.example/v1"},
        {"base_url": "https://attacker.example/v1"},
        {"api_key": "attacker-controlled-key"},
        {"user_config": {"model_list": []}},
        {"custom_llm_provider": "openai"},
        {"specific_deployment": True},
        {"deployment_id": "unreviewed-deployment"},
        {"model_group": "unreviewed-group"},
        {"model_list": [{"model_name": "unreviewed-model"}]},
        {"completion_model": "unreviewed-model"},
    ],
)
async def test_client_controlled_provider_routing_is_forbidden(routing_field: dict[str, object]) -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(
            request("raw.jwt.token", {"model": "general-fast", **routing_field}),
            "raw.jwt.token",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "client-controlled provider routing is not allowed"


async def test_empty_runtime_catalog_cannot_fall_through_to_raw_wildcards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "empty-runtime.yaml"
    write_runtime_config(runtime_config, [])
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        },
        developer_allow=["openrouter/*"],
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(
            request("raw.jwt.token", {"model": "openrouter/vendor/unapproved"}),
            "raw.jwt.token",
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


@pytest.mark.parametrize(
    "contents",
    [
        "model_list: [",
        "model_list: not-a-list\n",
        "model_list:\n  - model_name: 'openrouter/*'\n",
    ],
)
async def test_invalid_runtime_config_fails_closed(
    contents: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "invalid-runtime.yaml"
    runtime_config.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(request("raw.jwt.token"), "raw.jwt.token")

    assert caught.value.status_code == 503
    assert caught.value.detail == "authentication unavailable"


async def test_reset_runtime_caches_reloads_model_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "reload-runtime.yaml"
    write_runtime_config(runtime_config, ["general-first"])
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))
    claims = {
        "iss": "https://idp.example",
        "sub": "user-1",
        "exp": 4_000_000_000,
        "groups": ["developers"],
    }
    set_runtime(claims, developer_allow=["general-*"])
    first = await user_api_key_auth(request("raw.jwt.token"), "raw.jwt.token")
    assert first.models == ["general-first"]

    write_runtime_config(runtime_config, ["general-second"])
    reset_runtime_caches()
    set_runtime(claims, developer_allow=["general-*"])
    second = await user_api_key_auth(request("raw.jwt.token"), "raw.jwt.token")
    assert second.models == ["general-second"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/team/info"),
        ("POST", "/team/new"),
        ("POST", "/team/update"),
        ("GET", "/spend/logs/v2"),
        ("GET", "/spend/logs/ui/request-id"),
    ],
)
async def test_master_key_remains_available_for_managed_operations(
    method: str,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(tmp_path / "missing.yaml"))
    set_runtime({})
    result = await user_api_key_auth(
        request("admin-secret", path=path, method=method),
        "admin-secret",
    )
    assert str(result.user_role).endswith("proxy_admin")
    assert result.api_key == "litellm_proxy_master_key"
    assert result.api_key != "admin-secret"


@pytest.mark.parametrize(
    "path",
    [
        "/chat/completions",
        "/v1/chat/completions",
        "/models",
        "/v1/models",
        "/openai/deployments/provider/model/variant/chat/completions",
        "/fallback",
        "/config/update",
    ],
)
async def test_master_key_cannot_authorize_unmanaged_route(path: str) -> None:
    set_runtime({})

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(
            request("admin-secret", {"model": "general-fast"}, path=path),
            "admin-secret",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "the management credential is not allowed on this route"


async def test_master_route_decision_uses_dispatched_path_not_reconstructed_url() -> None:
    set_runtime({})
    forged = request("admin-secret", {"model": "general-fast"}, path="/v1/chat/completions")
    forged._url = URL("https://gateway.example/team/info")  # type: ignore[attr-defined]

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(forged, "admin-secret")

    assert caught.value.status_code == 403
    assert caught.value.detail == "the management credential is not allowed on this route"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/engines/general-fast"),
        ("POST", "/openai/deployments/general-fast/chat/completions"),
        ("POST", "/v1/models"),
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/responses"),
    ],
)
async def test_oidc_credentials_are_limited_to_the_reviewed_data_plane(method: str, path: str) -> None:
    set_runtime({})

    with pytest.raises(HTTPException) as caught:
        await user_api_key_auth(
            request("raw.jwt.token", {"model": "general-fast"}, path=path, method=method),
            "raw.jwt.token",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "OIDC credentials are not allowed on this route"
