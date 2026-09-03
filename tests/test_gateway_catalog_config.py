from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from gateway.catalog_config import RuntimeConfigError, render_runtime_config
from gateway.start import prepare_litellm_args, reject_alternate_config_sources, validate_auth_configuration

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
POLICY_FINGERPRINT = "a" * 64


def _write_base(path: Path) -> None:
    path.write_text(
        """
model_list:
  - model_name: general-fast
    litellm_params:
      model: os.environ/GENERAL_FAST_MODEL
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
""".lstrip(),
        encoding="utf-8",
    )


def _artifact(
    models: list[dict[str, Any]],
    *,
    generated_at: str = "2026-09-02T11:59:00+00:00",
    policy_fingerprint: str = POLICY_FINGERPRINT,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "policy_fingerprint": policy_fingerprint,
        "source": "/api/v1/models/user",
        "stale": False,
        "models": models,
    }


def _model(model_id: str, prompt: float = 1.25, completion: float = 2.5) -> dict[str, Any]:
    return {
        "id": model_id,
        "pricing_usd_per_million": {"prompt": prompt, "completion": completion},
    }


def _read_model_list(path: Path) -> list[dict[str, Any]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    models = value["model_list"]
    assert isinstance(models, list)
    return models


def test_checked_in_config_exposes_no_unrestricted_wildcard_route() -> None:
    config_path = Path(__file__).parents[1] / "gateway" / "litellm" / "config.yaml"
    models = _read_model_list(config_path)

    route_names = [str(item["model_name"]) for item in models]
    assert "openrouter/*" not in route_names
    assert all("*" not in name for name in route_names)


def test_fresh_catalog_adds_sorted_priced_routes_and_preserves_static_routes(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    catalog = tmp_path / "approved-models.json"
    output = tmp_path / "runtime.yaml"
    _write_base(base)
    payload = _artifact(
        [
            _model("vendor/z-model"),
            _model("vendor/a-model", prompt=0, completion=0),
            _model("openrouter/auto"),
        ]
    )
    artifact_text = json.dumps(payload)
    catalog.write_text(artifact_text, encoding="utf-8")

    result = render_runtime_config(
        base,
        catalog,
        output,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW,
    )

    assert result.catalog_state == "fresh"
    assert result.added_models == 3
    assert catalog.read_text(encoding="utf-8") == artifact_text
    models = _read_model_list(output)
    assert [item["model_name"] for item in models] == [
        "general-fast",
        "openrouter/openrouter/auto",
        "openrouter/vendor/a-model",
        "openrouter/vendor/z-model",
    ]
    assert models[1]["litellm_params"]["model"] == "openrouter/openrouter/auto"
    assert models[2]["model_info"] == {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
    assert models[3]["litellm_params"] == {
        "model": "openrouter/vendor/z-model",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "os.environ/OPENROUTER_API_KEY",
    }
    assert models[3]["model_info"]["input_cost_per_token"] == pytest.approx(0.00000125)
    assert models[3]["model_info"]["output_cost_per_token"] == pytest.approx(0.0000025)


@pytest.mark.parametrize(
    ("catalog_contents", "expected_state"),
    [
        (None, "missing"),
        ("not json", "invalid"),
        (json.dumps(_artifact([_model("vendor/model")], generated_at="2026-08-01T00:00:00Z")), "stale"),
        (json.dumps(_artifact([_model("vendor/model", prompt=float("nan"))])), "invalid"),
        (json.dumps(_artifact([_model("vendor/*")])), "invalid"),
        (json.dumps(_artifact([_model("vendor/model,general-fast")])), "invalid"),
        (json.dumps(_artifact([_model("vendor/model")], policy_fingerprint="b" * 64)), "invalid"),
    ],
)
def test_unusable_catalog_falls_back_to_aliases_only(
    tmp_path: Path,
    catalog_contents: str | None,
    expected_state: str,
) -> None:
    base = tmp_path / "base.yaml"
    catalog = tmp_path / "approved-models.json"
    output = tmp_path / "runtime.yaml"
    _write_base(base)
    output.write_text("model_list:\n  - model_name: stale-route\n", encoding="utf-8")
    if catalog_contents is not None:
        catalog.write_text(catalog_contents, encoding="utf-8")

    result = render_runtime_config(
        base,
        catalog,
        output,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW,
    )

    assert result.catalog_state == expected_state
    assert result.added_models == 0
    assert [item["model_name"] for item in _read_model_list(output)] == ["general-fast"]


def test_malformed_static_config_fails_startup(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("model_list: not-a-list\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="model_list"):
        render_runtime_config(
            base,
            tmp_path / "missing.json",
            tmp_path / "runtime.yaml",
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("arguments", "expected_base", "expected_arguments"),
    [
        (
            ["--config", "/static/config.yaml", "--port", "4000"],
            "/static/config.yaml",
            ["--config", "/rendered/runtime.yaml", "--port", "4000"],
        ),
        (
            ["--port", "4000", "--config=/static/config.yaml"],
            "/static/config.yaml",
            ["--port", "4000", "--config=/rendered/runtime.yaml"],
        ),
        (
            ["--port", "4000"],
            "/default/config.yaml",
            ["--port", "4000", "--config", "/rendered/runtime.yaml"],
        ),
    ],
)
def test_start_wrapper_rewrites_or_adds_config_argument(
    arguments: list[str],
    expected_base: str,
    expected_arguments: list[str],
) -> None:
    base, rewritten = prepare_litellm_args(
        arguments,
        runtime_config="/rendered/runtime.yaml",
        default_base_config="/default/config.yaml",
    )

    assert base == Path(expected_base)
    assert rewritten == expected_arguments


@pytest.mark.parametrize(
    "name",
    [
        "CONFIG_FILE_PATH",
        "LITELLM_CONFIG_BUCKET_NAME",
        "LITELLM_CONFIG_BUCKET_OBJECT_KEY",
        "LITELLM_CONFIG_BUCKET_TYPE",
        "WORKER_CONFIG",
    ],
)
def test_start_wrapper_rejects_alternate_upstream_config_sources(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(name, "")

    with pytest.raises(RuntimeConfigError, match=name):
        reject_alternate_config_sources()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("OIDC_ALLOW_HTTP", "sometimes", "must be a boolean"),
        ("OIDC_ALLOWED_ALGORITHMS", "HS256", "only asymmetric"),
        ("OIDC_JWKS_TTL_SECONDS", "not-an-integer", "must be an integer"),
    ],
)
def test_start_wrapper_rejects_invalid_oidc_settings_before_serving(
    name: str,
    value: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example/litellm")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-only-master-key")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "enterprise-ai-gateway")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        validate_auth_configuration()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", None),
        ("DATABASE_URL", ""),
        ("DATABASE_URL", "   "),
        ("LITELLM_MASTER_KEY", None),
        ("LITELLM_MASTER_KEY", ""),
        ("LITELLM_MASTER_KEY", " test-key"),
    ],
)
def test_start_wrapper_requires_resolved_database_and_master_key_settings(
    name: str,
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example/litellm")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-only-master-key")
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        validate_auth_configuration()
