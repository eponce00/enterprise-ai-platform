from pathlib import Path
from typing import Any

import yaml

OVERRIDE = Path(__file__).parents[1] / "infra" / "production" / "compose.yaml"
REQUIRED_IMAGE = "${GATEWAY_IMAGE:?set GATEWAY_IMAGE to a registry image pinned by sha256 digest}"


class ComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that understands Compose's value-reset tag."""


def _reset(_loader: yaml.SafeLoader, _node: yaml.Node) -> None:
    return None


ComposeLoader.add_constructor("!reset", _reset)


def _services() -> dict[str, Any]:
    document = yaml.load(OVERRIDE.read_text(encoding="utf-8"), Loader=ComposeLoader)  # noqa: S506
    assert isinstance(document, dict)
    services = document["services"]
    assert isinstance(services, dict)
    return services


def test_production_services_share_promoted_image_without_build() -> None:
    raw = OVERRIDE.read_text(encoding="utf-8")
    services = _services()

    assert "build: !reset null" in raw
    assert "env_file: !reset []" in raw
    assert services["gateway"]["build"] is None
    assert services["gateway"]["env_file"] is None
    assert services["gateway"]["environment"]["DATABASE_URL"] == (
        "${DATABASE_URL:?set a secret-managed PostgreSQL URL with percent-encoded credentials}"
    )
    for service in ("gateway", "bootstrap", "catalog-sync"):
        assert services[service]["image"] == REQUIRED_IMAGE


def test_catalog_sync_runs_from_promoted_image_and_publishes_to_bind_mount() -> None:
    service = _services()["catalog-sync"]

    assert service["entrypoint"] == ["python", "-m", "catalog.sync"]
    assert service["command"][-1] == "/app/catalog/generated/approved-models.json"
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": "../catalog/generated",
            "target": "/app/catalog/generated",
        }
    ]
