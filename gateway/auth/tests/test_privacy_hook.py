from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gateway.auth.privacy_hook import EnterprisePolicyHook


async def test_privacy_policy_can_only_be_narrowed(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_POLICY_MODELS", "openrouter/*")
    auth = SimpleNamespace(
        metadata={
            "openrouter_policy": {
                "zdr": True,
                "data_collection": "deny",
                "provider_allowlist": ["approved-a", "approved-b"],
                "provider_denylist": ["denied"],
            }
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


async def test_direct_provider_is_not_given_openrouter_parameters(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_POLICY_MODELS", "openrouter/*")
    auth = SimpleNamespace(metadata={"openrouter_policy": {"zdr": True}})
    data = {"model": "general-fast"}
    assert await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion") == data
    assert "provider" not in data


async def test_disjoint_provider_narrowing_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_POLICY_MODELS", "openrouter/*")
    auth = SimpleNamespace(metadata={"openrouter_policy": {"provider_allowlist": ["approved"]}})
    data = {
        "model": "openrouter/vendor/model",
        "extra_body": {"provider": {"only": ["unapproved"], "zdr": False}},
    }
    with pytest.raises(HTTPException) as caught:
        await EnterprisePolicyHook().async_pre_call_hook(auth, None, data, "acompletion")
    assert caught.value.status_code == 403
