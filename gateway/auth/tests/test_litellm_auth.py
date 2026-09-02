from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException, Request

import gateway.auth.oidc_auth as auth_module
from gateway.auth.oidc import OIDCUnavailableError
from gateway.auth.oidc_auth import reset_runtime_caches, user_api_key_auth
from gateway.auth.policy import PolicyEngine


class StaticValidator:
    def __init__(self, claims: dict[str, Any]):
        self.claims = claims

    async def validate(self, _: str) -> dict[str, Any]:
        return self.claims


def request(token: str, body: dict[str, Any] | None = None) -> Request:
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
            "method": "POST",
            "path": "/v1/chat/completions",
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


def write_runtime_config(path: Path, models: list[str]) -> None:
    path.write_text(
        yaml.safe_dump({"model_list": [{"model_name": model} for model in models]}),
        encoding="utf-8",
    )


def set_runtime(
    claims: dict[str, Any],
    *,
    developer_allow: list[str] | None = None,
    developer_deny: list[str] | None = None,
) -> None:
    developer_models: dict[str, list[str]] = {
        "allow": ["general-fast"] if developer_allow is None else developer_allow
    }
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
                "developers": {"monthly_budget_usd": 20, "models": developer_models},
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
    assert config["general_settings"]["store_prompts_in_spend_logs"] is False
    assert config["litellm_settings"]["enable_post_custom_auth_checks"] is True
    assert config["litellm_settings"]["turn_off_message_logging"] is True


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
    # but require Prisma to provide team models, spend, budget, and rate limits.
    assert result.models == ["general-fast"]
    assert result.team_models == []
    assert getattr(result, "team_max_budget", None) is None
    assert getattr(result, "team_rpm_limit", None) is None
    assert getattr(result, "team_tpm_limit", None) is None


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


async def test_master_key_remains_available_for_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LITELLM_RUNTIME_CONFIG", str(tmp_path / "missing.yaml"))
    set_runtime({})
    result = await user_api_key_auth(request("admin-secret"), "admin-secret")
    assert str(result.user_role).endswith("proxy_admin")
