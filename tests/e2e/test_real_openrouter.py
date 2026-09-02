from __future__ import annotations

import os

import pytest
from openai import OpenAI

pytestmark = [pytest.mark.integration, pytest.mark.real_provider]


def _real_provider_environment() -> tuple[str, list[str]]:
    if os.getenv("RUN_REAL_PROVIDER_TESTS") != "1":
        pytest.skip("set RUN_REAL_PROVIDER_TESTS=1 to opt in to billable real-provider tests")

    oidc_token = os.getenv("REAL_OIDC_TOKEN", "").strip()
    if not oidc_token:
        pytest.skip(
            "set REAL_OIDC_TOKEN to an explicit, short-lived OIDC access token; "
            "the development service-token fixture is intentionally not accepted"
        )

    raw_model_ids = os.getenv("REAL_MODEL_IDS", "").strip()
    if not raw_model_ids:
        pytest.skip("set REAL_MODEL_IDS to at least five comma-separated, currently approved OpenRouter model IDs")

    models = [item.strip() for item in raw_model_ids.split(",") if item.strip()]
    assert len(models) >= 5, "provide at least five currently approved, unrelated model families"
    invalid = [model for model in models if "/" not in model or model.startswith("openrouter/")]
    assert not invalid, f"use OpenRouter provider/model IDs without the gateway prefix: {invalid}"
    families = {model.split("/", 1)[0] for model in models}
    assert len(families) >= 5, "provide model IDs from at least five distinct provider namespaces"
    return oidc_token, models


def test_representative_openrouter_families(gateway_url: str) -> None:
    oidc_token, models = _real_provider_environment()
    with OpenAI(base_url=gateway_url, api_key=oidc_token, timeout=60, max_retries=1) as client:
        for model in models:
            result = client.chat.completions.create(
                model=f"openrouter/{model}",
                messages=[{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=8,
            )
            assert result.choices[0].message.content
