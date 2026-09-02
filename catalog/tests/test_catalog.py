from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from catalog.sync.openrouter import (
    CatalogPolicy,
    CatalogSyncError,
    OpenRouterCatalogClient,
    filter_models,
    sync_catalog,
)


def model(model_id: str, **overrides):
    value = {
        "id": model_id,
        "name": model_id,
        "context_length": 128_000,
        "supported_parameters": ["tools"],
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }
    value.update(overrides)
    return value


def test_new_removed_and_canonical_model_names() -> None:
    policy = CatalogPolicy()
    first, _ = filter_models([model("vendor/old"), model("vendor/new", canonical_slug="vendor/permanent")], policy)
    second, _ = filter_models([model("vendor/new", canonical_slug="vendor/permanent")], policy)
    assert {item["id"] for item in first} - {item["id"] for item in second} == {"vendor/old"}
    assert second[0]["canonical_slug"] == "vendor/permanent"


def test_policy_rejects_capability_price_and_expired_models() -> None:
    policy = CatalogPolicy(require_tools=True, max_prompt_usd_per_million=2)
    accepted, rejected = filter_models(
        [
            model("ok/model"),
            model("no-tools/model", supported_parameters=[]),
            model("expensive/model", pricing={"prompt": "0.000010", "completion": "0"}),
            model("unknown-price/model", pricing={"prompt": None, "completion": "0"}),
            model("nan-price/model", pricing={"prompt": "NaN", "completion": "0"}),
            model("negative-price/model", pricing={"prompt": "-0.000001", "completion": "0"}),
            model("bad-pricing/model", pricing=["not", "a", "mapping"]),
            model("bad-architecture/model", architecture=["not", "a", "mapping"]),
            model("bad-context/model", context_length="many"),
            model("wildcard/*"),
            model("deprecated/model", expiration_date="2020-01-01"),
        ],
        policy,
    )
    assert [item["id"] for item in accepted] == ["ok/model"]
    assert rejected == {"tools": 1, "prompt_price": 1, "invalid": 7, "expired": 1}


@pytest.mark.parametrize("ceiling", ["NaN", "Infinity", -1])
def test_policy_rejects_non_finite_or_negative_price_ceiling(tmp_path: Path, ceiling: object) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(f"catalog:\n  max_prompt_usd_per_million: {ceiling}\n", encoding="utf-8")

    with pytest.raises(CatalogSyncError, match="finite and non-negative"):
        CatalogPolicy.from_file(policy)


def test_policy_normalizes_scalar_patterns_without_splitting_characters(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "catalog:\n  allow: vendor/*\n  deny: vendor/revoked\n  required_input_modalities: text\n",
        encoding="utf-8",
    )

    policy = CatalogPolicy.from_file(policy_path)

    assert policy.allow == ("vendor/*",)
    assert policy.deny == ("vendor/revoked",)
    assert policy.required_input_modalities == ("text",)
    accepted, rejected = filter_models([model("vendor/allowed"), model("vendor/revoked")], policy)
    assert [item["id"] for item in accepted] == ["vendor/allowed"]
    assert rejected == {"denied": 1}


class StaticClient(OpenRouterCatalogClient):
    def __init__(self, models=None, error=False, api_key=None):
        super().__init__(api_key=api_key)
        self.models = models or []
        self.error = error
        self.required_zdr = None

    def fetch(self, require_zdr=True):
        self.required_zdr = require_zdr
        if self.error:
            raise CatalogSyncError("outage")
        return self.models


@pytest.mark.parametrize("content", [b"not-json", b"[]"])
def test_client_rejects_malformed_catalog_response(content: bytes) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    with httpx.Client(base_url="https://openrouter.example", transport=transport) as http_client:
        client = OpenRouterCatalogClient(client=http_client)
        with pytest.raises(CatalogSyncError, match="invalid"):
            client.fetch()


def test_privacy_filter_is_requested_and_live_result_is_atomic(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    client = StaticClient([model("vendor/model")], api_key="server-key")
    result = sync_catalog(CatalogPolicy(require_zdr=True), output, client=client, now=1000)
    assert client.required_zdr is True
    assert result.source == "/api/v1/models/user"
    assert json.loads(output.read_text())["models"][0]["id"] == "vendor/model"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are required")
@pytest.mark.parametrize("replace_existing", [False, True])
def test_published_catalog_is_world_readable(tmp_path: Path, replace_existing: bool) -> None:
    output = tmp_path / "catalog.json"
    if replace_existing:
        output.write_text('{"previous": true}\n', encoding="utf-8")
        output.chmod(0o600)

    sync_catalog(CatalogPolicy(), output, client=StaticClient([model("vendor/model")]), now=1000)

    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert json.loads(output.read_text(encoding="utf-8"))["models"][0]["id"] == "vendor/model"
    assert not list(tmp_path.glob(".catalog.json.*"))


def test_catalog_outage_uses_only_fresh_cache(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    policy = CatalogPolicy(max_stale_seconds=100)
    sync_catalog(policy, output, client=StaticClient([model("vendor/cached")]), now=1000)
    os.utime(output, (1050, 1050))
    result = sync_catalog(policy, output, client=StaticClient(error=True), now=1050)
    assert result.stale is True
    assert result.models[0]["id"] == "vendor/cached"
    with pytest.raises(CatalogSyncError, match="outage"):
        sync_catalog(CatalogPolicy(max_stale_seconds=10), output, client=StaticClient(error=True), now=1050)


def test_catalog_outage_rejects_cache_from_an_old_policy(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    sync_catalog(CatalogPolicy(), output, client=StaticClient([model("vendor/revoked")]), now=1000)

    changed_policy = CatalogPolicy(deny=("vendor/revoked",))
    with pytest.raises(CatalogSyncError, match="outage"):
        sync_catalog(changed_policy, output, client=StaticClient(error=True), now=1050)


def test_touching_an_expired_or_malformed_cache_cannot_revive_it(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    policy = CatalogPolicy(max_stale_seconds=10)
    sync_catalog(policy, output, client=StaticClient([model("vendor/cached")]), now=1000)
    os.utime(output, (10_000, 10_000))

    with pytest.raises(CatalogSyncError, match="outage"):
        sync_catalog(policy, output, client=StaticClient(error=True), now=1050)

    output.write_text(json.dumps({"schema_version": 1, "generated_at": "not-a-date", "models": [{}]}))
    with pytest.raises(CatalogSyncError, match="outage"):
        sync_catalog(CatalogPolicy(max_stale_seconds=100), output, client=StaticClient(error=True), now=1050)
