from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gateway.auth.privacy_hook import EnterprisePolicyHook


async def test_privacy_policy_can_only_be_narrowed() -> None:
    auth = SimpleNamespace(
        metadata={
            "route_backend": "openrouter",
            "openrouter_policy": {
                "zdr": True,
                "data_collection": "deny",
                "provider_allowlist": ["approved-a", "approved-b"],
                "provider_denylist": ["denied"],
            },
        }
    )
    data = {
        "model": "openrouter/vendor/model",
        "tools": [{"type": "function"}],
        "provider": {"zdr": False, "data_collection": "allow", "only": ["approved-b", "other"]},
    }
    result = await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")
    assert result["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "only": ["approved-b"],
        "ignore": ["denied"],
        "require_parameters": True,
    }


async def test_direct_provider_is_not_given_openrouter_parameters() -> None:
    auth = SimpleNamespace(metadata={"route_backend": "direct", "openrouter_policy": {"zdr": True}})
    data = {"model": "general-fast"}
    assert await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion") == data
    assert "provider" not in data


async def test_disjoint_provider_narrowing_fails_closed() -> None:
    auth = SimpleNamespace(
        metadata={"route_backend": "openrouter", "openrouter_policy": {"provider_allowlist": ["approved"]}}
    )
    data = {
        "model": "openrouter/vendor/model",
        "extra_body": {"provider": {"only": ["unapproved"], "zdr": False}},
    }
    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")
    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "data",
    [
        {"model": "openrouter/vendor/model", "provider": {"order": ["unreviewed"]}},
        {"model": "openrouter/vendor/model", "extra_body": {"provider": {"allow_fallbacks": True}}},
        {"model": "openrouter/vendor/model", "provider": "unreviewed"},
    ],
)
async def test_unreviewed_or_invalid_openrouter_provider_controls_fail_closed(
    data: dict[str, object],
) -> None:
    auth = SimpleNamespace(
        metadata={
            "route_backend": "openrouter",
            "openrouter_policy": {"zdr": True, "data_collection": "deny"},
        }
    )

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")

    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "data",
    [
        {"model": "general-fast", "provider": {"only": ["provider-a"]}},
        {"model": "general-fast", "extra_body": {"provider": {"ignore": ["provider-b"]}}},
    ],
)
async def test_direct_routes_reject_provider_controls(data: dict[str, object]) -> None:
    auth = SimpleNamespace(metadata={"route_backend": "direct", "openrouter_policy": {}})

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")

    assert caught.value.status_code == 403
    assert caught.value.detail == "provider controls are only allowed for OpenRouter routes"


@pytest.mark.parametrize(
    "override",
    [
        {"fallbacks": [{"general-fast": ["unreviewed-model"]}]},
        {"model_group_alias": {"general-fast": "unreviewed-model"}},
        ["invalid-non-mapping-value"],
    ],
)
async def test_database_router_override_fails_closed(override: object) -> None:
    auth = SimpleNamespace(metadata={"route_backend": "direct"})
    data = {"model": "general-fast", "router_settings_override": override}

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")

    assert caught.value.status_code == 403
    assert caught.value.detail == "database router overrides are not allowed"


async def test_empty_database_router_override_is_harmless() -> None:
    auth = SimpleNamespace(metadata={"route_backend": "direct"})
    data = {"model": "general-fast", "router_settings_override": {}}

    assert await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion") == data


async def test_database_model_aliases_fail_closed() -> None:
    auth = SimpleNamespace(
        metadata={"route_backend": "direct"},
        aliases={"general-fast": "unreviewed-model"},
    )

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(
            auth,
            None,
            {"model": "unreviewed-model"},
            "acompletion",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "database model aliases are not allowed"


async def test_team_database_model_aliases_fail_closed() -> None:
    auth = SimpleNamespace(
        metadata={"route_backend": "direct"},
        team_model_aliases={"general-fast": "unreviewed-model"},
    )

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(
            auth,
            None,
            {"model": "unreviewed-model"},
            "acompletion",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "database model aliases are not allowed"


async def test_post_auth_model_rewrite_fails_closed() -> None:
    auth = SimpleNamespace(
        metadata={
            "authorized_model": "general-fast",
            "route_backend": "direct",
        }
    )

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(
            auth,
            None,
            {"model": "unreviewed-model"},
            "acompletion",
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "model routing changed after authorization"


@pytest.mark.parametrize(
    "data",
    [
        {"model": "general-fast", "models": ["unreviewed-model"]},
        {"model": "general-fast", "route": "fallback"},
        {"model": "general-fast", "routing_strategy": "least-busy"},
        {"model": "general-fast", "api_base": "https://attacker.example/v1"},
        {"model": "general-fast", "extra_body": {"model": "unreviewed-model"}},
        {"model": "general-fast", "extra_body": {"models": ["unreviewed-model"]}},
        {"model": "general-fast", "extra_body": {"route": "fallback"}},
        {"model": "general-fast", "extra_body": {"routing_strategy": "least-busy"}},
        {"model": "general-fast", "extra_body": {"nested": {"model": "unreviewed-model"}}},
        {"model": "general-fast", "extra_body": [["model", "unreviewed-model"]]},
    ],
)
async def test_client_routing_controls_fail_closed_before_dispatch(data: dict[str, object]) -> None:
    auth = SimpleNamespace(
        metadata={
            "authorized_model": "general-fast",
            "route_backend": "direct",
        }
    )

    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")

    assert caught.value.status_code == 403
    assert caught.value.detail == "client-controlled provider routing is not allowed"
