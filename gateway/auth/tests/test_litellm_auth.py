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
            secure_runtime_document(
                {"model_list": [{"model_name": model, "litellm_params": {"model": target}} for model in models]}
            )
        ),
        encoding="utf-8",
    )


def secure_runtime_document(document: dict[str, Any]) -> dict[str, Any]:
    for entry in document.get("model_list", []):
        params = entry.get("litellm_params", {})
        if "api_base" not in params and "base_url" not in params:
            params["api_base"] = "https://direct.example/v1"
    return {
        "litellm_settings": {
            "callbacks": ["gateway.auth.privacy_hook.enterprise_policy_hook"],
            "drop_params": True,
            "enable_post_custom_auth_checks": True,
            "failure_callback": ["prometheus"],
            "global_disable_no_log_param": True,
            "redact_user_api_key_info": True,
            "set_verbose": False,
            "success_callback": ["prometheus"],
            "turn_off_message_logging": True,
        },
        "general_settings": {
            "allow_requests_on_db_unavailable": False,
            "custom_auth": "gateway.auth.oidc_auth.user_api_key_auth",
            "custom_auth_run_common_checks": True,
            "database_url": "os.environ/DATABASE_URL",
            "disable_spend_logs": False,
            "fail_closed_budget_enforcement": True,
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "store_model_in_db": False,
            "store_prompts_in_spend_logs": False,
            "supported_db_objects": [],
            "user_api_key_cache_ttl": 5,
        },
        **document,
    }


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


def test_shipped_litellm_config_passes_the_startup_policy_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path(__file__).parents[2] / "litellm" / "config.yaml"
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(config_path))
    for prefix in ("GENERAL_FAST", "GENERAL_QUALITY", "CODING_FAST", "CODING_FRONTIER", "CHEAP_BATCH"):
        monkeypatch.setenv(f"{prefix}_MODEL", "openrouter/vendor/model")
        monkeypatch.setenv(f"{prefix}_API_BASE", "https://openrouter.ai/api/v1")
        monkeypatch.setenv(f"{prefix}_API_KEY", "test-only-placeholder")

    validate_runtime_model_policy()


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
            secure_runtime_document(
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
            )
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
        ("openai/gpt-4o", "https://openrouter\u3002ai/api/v1"),
        ("openai/gpt-4o", "https://openrouter\uff0eai/api/v1"),
        ("openai/gpt-4o", "https://openrouter\uff61ai/api/v1"),
        ("openrouter/openai/gpt-4o", "http://openrouter.ai/api/v1"),
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
        yaml.safe_dump(
            secure_runtime_document({"model_list": [{"model_name": "general-fast", "litellm_params": params}]})
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


@pytest.mark.parametrize("target", ["openai/gpt-4o", "openrouter/openai/gpt-4o"])
def test_every_deployment_requires_an_explicit_api_base(
    target: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = secure_runtime_document(
        {"model_list": [{"model_name": "general-fast", "litellm_params": {"model": target}}]}
    )
    del document["model_list"][0]["litellm_params"]["api_base"]
    runtime_config = tmp_path / "implicit-api-base-runtime.yaml"
    runtime_config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="declare api_base or base_url"):
        validate_runtime_model_policy()


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
        {"model": "openai/gpt-4o", "models": ["unreviewed-model"]},
        {"model": "openai/gpt-4o", "route": "fallback"},
        {"model": "openai/gpt-4o", "routing_strategy": "least-busy"},
        {"model": "openai/gpt-4o", "configurable_clientside_auth_params": ["api_base"]},
        {"model": "openai/gpt-4o", "extra_body": {"model": "unreviewed-model"}},
        {"model": "openai/gpt-4o", "extra_body": {"nested": {"route": "fallback"}}},
        {"model": "openai/gpt-4o", "extra_body": [["model", "unreviewed-model"]]},
        {"model": "auto_router/complexity_router"},
        {"model": "openai/gpt-4o", "quality_router_config": {"tiers": {}}},
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
        yaml.safe_dump(
            secure_runtime_document({"model_list": [{"model_name": "general-fast", "litellm_params": extra_params}]})
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
    "model_info",
    [
        {"id": "general-fast"},
        {"team_id": "unreviewed-team"},
        {"access_via_team_ids": ["unreviewed-team"]},
        {"direct_access": True},
        {"team_public_model_name": "general-fast"},
    ],
)
def test_model_info_cannot_change_deployment_routing_or_access(
    model_info: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "unsafe-model-info-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openai/gpt-4o"},
                            "model_info": model_info,
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="model_info"):
        validate_runtime_model_policy()


def test_catalog_cost_metadata_is_reviewed_model_info(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "cost-metadata-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openai/gpt-4o"},
                            "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    validate_runtime_model_policy()


def test_catalog_cost_metadata_cannot_omit_one_token_direction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "partial-cost-metadata-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openai/gpt-4o"},
                            "model_info": {"input_cost_per_token": 1e-6},
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="both model_info cost fields"):
        validate_runtime_model_policy()


@pytest.mark.parametrize("cost", [-1, float("nan"), True, "0.000001"])
def test_catalog_cost_metadata_must_be_a_nonnegative_finite_number(
    cost: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "invalid-cost-metadata-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openai/gpt-4o"},
                            "model_info": {"input_cost_per_token": cost},
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="input_cost_per_token"):
        validate_runtime_model_policy()


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("top_level", "credential_list", [{"credential_name": "unreviewed"}]),
        ("top_level", "environment_variables", {"GENERAL_FAST_MODEL": "openrouter/vendor/model"}),
        ("model_entry", "tpm", 1),
        ("litellm_params", "litellm_credential_name", "unreviewed"),
        ("litellm_params", "timeout", 30),
    ],
)
def test_unreviewed_runtime_config_surfaces_fail_startup(
    scope: str,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = secure_runtime_document(
        {"model_list": [{"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}}]}
    )
    if scope == "top_level":
        document[field] = value
    elif scope == "model_entry":
        document["model_list"][0][field] = value
    else:
        document["model_list"][0]["litellm_params"][field] = value
    runtime_config = tmp_path / "unreviewed-surface-runtime.yaml"
    runtime_config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match=field):
        validate_runtime_model_policy()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_fails", True),
        ("allowed_fails", 101),
        ("num_retries", -1),
        ("num_retries", 1.5),
        ("cooldown_time", float("nan")),
        ("cooldown_time", 3601),
        ("retry_after", -1),
        ("retry_after", "1"),
    ],
)
def test_router_operational_limits_fail_startup_when_invalid(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "invalid-router-limit-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [{"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}}],
                    "router_settings": {field: value},
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match=field):
        validate_runtime_model_policy()


@pytest.mark.parametrize(
    "api_key",
    [{"secret": "value"}, "", " leading-space", "os.environ/MISSING_TEST_API_KEY"],
)
def test_deployment_api_key_must_be_a_nonempty_scalar(
    api_key: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "invalid-api-key-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openai/gpt-4o", "api_key": api_key},
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="api_key"):
        validate_runtime_model_policy()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("router_settings", "default_litellm_params", {"api_base": "https://attacker.example/v1"}),
        ("router_settings", "routing_strategy", "usage-based-routing-v2"),
        ("litellm_settings", "model_fallbacks", [{"general-fast": ["unreviewed-model"]}]),
        ("litellm_settings", "model_alias_map", {"general-fast": "unreviewed-model"}),
        ("litellm_settings", "api_base", "https://attacker.example/v1"),
        ("general_settings", "completion_model", "unreviewed-model"),
        ("general_settings", "infer_model_from_keys", True),
        ("general_settings", "pass_through_all_models", True),
    ],
)
def test_unreviewed_global_or_router_settings_fail_startup(
    section: str,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "unsafe-global-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [{"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}}],
                    section: {field: value},
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match=field):
        validate_runtime_model_policy()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("general_settings", "store_model_in_db", True),
        ("general_settings", "supported_db_objects", ["models"]),
        ("general_settings", "custom_auth", "unreviewed.auth"),
        ("general_settings", "allow_requests_on_db_unavailable", True),
        ("general_settings", "allow_requests_on_db_unavailable", 0),
        ("general_settings", "fail_closed_budget_enforcement", 1),
        ("general_settings", "user_api_key_cache_ttl", 6),
        ("litellm_settings", "callbacks", []),
        ("litellm_settings", "enable_post_custom_auth_checks", False),
        ("litellm_settings", "turn_off_message_logging", False),
    ],
)
def test_security_critical_runtime_values_fail_startup_when_weakened(
    section: str,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = secure_runtime_document(
        {"model_list": [{"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}}]}
    )
    document[section][field] = value
    runtime_config = tmp_path / "weakened-runtime.yaml"
    runtime_config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match=field):
        validate_runtime_model_policy()


@pytest.mark.parametrize("missing", ["litellm_settings", "general_settings"])
def test_required_security_section_cannot_be_removed(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = secure_runtime_document(
        {"model_list": [{"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}}]}
    )
    del document[missing]
    runtime_config = tmp_path / "missing-security-section-runtime.yaml"
    runtime_config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match=missing):
        validate_runtime_model_policy()


def test_public_model_name_cannot_collide_with_generated_deployment_id_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "deployment-id-collision-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {"model_list": [{"model_name": "a" * 64, "litellm_params": {"model": "openai/gpt-4o"}}]}
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="deployment ID namespace"):
        validate_runtime_model_policy()


def test_public_model_name_cannot_enable_litellm_comma_batch_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "comma-batch-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {
                            "model_name": "unreviewed,general-fast",
                            "litellm_params": {"model": "openai/gpt-4o"},
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(runtime_config))

    with pytest.raises(RuntimeModelPolicyError, match="non-exact model_name"):
        validate_runtime_model_policy()


async def test_duplicate_runtime_model_cannot_mix_backend_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "mixed-runtime.yaml"
    runtime_config.write_text(
        yaml.safe_dump(
            secure_runtime_document(
                {
                    "model_list": [
                        {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o"}},
                        {
                            "model_name": "general-fast",
                            "litellm_params": {"model": "openrouter/openai/gpt-4o-mini"},
                        },
                    ]
                }
            )
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
            secure_runtime_document(
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
            )
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
            secure_runtime_document(
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
            )
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
            secure_runtime_document(
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
            )
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
            secure_runtime_document(
                {
                    "model_list": [
                        {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                        {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                    ],
                    "router_settings": {"fallbacks": fallbacks},
                }
            )
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
            secure_runtime_document(
                {
                    "model_list": [
                        {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                        {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                    ],
                    "router_settings": {"model_group_alias": {"general-fast": "general-quality"}},
                }
            )
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
            secure_runtime_document(
                {
                    "model_list": [
                        {"model_name": "general-fast", "litellm_params": {"model": "openai/gpt-4o-mini"}},
                        {"model_name": "general-quality", "litellm_params": {"model": "openai/gpt-4o"}},
                    ],
                    "router_settings": {"fallbacks": [{"general-fast": ["general-quality"]}]},
                }
            )
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
        {"configurable_clientside_auth_params": ["api_base"]},
        {"models": ["unreviewed-model"]},
        {"route": "fallback"},
        {"routing_strategy": "least-busy"},
        {"extra_body": {"model": "unreviewed-model"}},
        {"extra_body": {"models": ["unreviewed-model"]}},
        {"extra_body": {"route": "fallback"}},
        {"extra_body": {"routing_strategy": "least-busy"}},
        {"extra_body": {"nested": {"model": "unreviewed-model"}}},
        {"extra_body": [["model", "unreviewed-model"]]},
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


async def test_non_routing_extra_body_is_allowed() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )

    result = await user_api_key_auth(
        request(
            "raw.jwt.token",
            {
                "model": "general-fast",
                "extra_body": {"safe_extension": True},
            },
        ),
        "raw.jwt.token",
    )

    assert result.metadata["authorized_model"] == "general-fast"


async def test_tool_schema_property_named_provider_is_not_a_routing_control() -> None:
    set_runtime(
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "exp": 4_000_000_000,
            "groups": ["developers"],
        }
    )
    body = {
        "model": "general-fast",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"provider": {"type": "string"}},
                    },
                },
            }
        ],
    }

    result = await user_api_key_auth(request("raw.jwt.token", body), "raw.jwt.token")

    assert result.metadata["authorized_model"] == "general-fast"


@pytest.mark.parametrize(
    "provider_controls",
    [
        {"provider": {"only": ["provider-a"]}},
        {"extra_body": {"provider": {"ignore": ["provider-b"]}}},
    ],
)
async def test_direct_route_rejects_openrouter_provider_controls(
    provider_controls: dict[str, object],
) -> None:
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
            request("raw.jwt.token", {"model": "general-fast", **provider_controls}),
            "raw.jwt.token",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "provider controls are only allowed for OpenRouter routes"


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
