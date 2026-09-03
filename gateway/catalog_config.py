"""Render approved OpenRouter catalog entries into a LiteLLM runtime config."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

CatalogState = Literal["fresh", "missing", "stale", "invalid"]
_OPENROUTER_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/\S+$")
_GLOB_META = frozenset("*?[]")
_PER_MILLION = 1_000_000
_MAX_FUTURE_SKEW_SECONDS = 300
_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class RuntimeConfigError(RuntimeError):
    """Raised when the static LiteLLM config cannot be rendered safely."""


class CatalogArtifactError(ValueError):
    """Raised when an approved-catalog artifact is malformed."""


class StaleCatalogArtifactError(CatalogArtifactError):
    """Raised when an approved-catalog artifact is outside its freshness window."""


@dataclass(frozen=True)
class RenderResult:
    output_path: Path
    catalog_state: CatalogState
    added_models: int
    detail: str


def render_runtime_config(
    base_config_path: str | Path,
    catalog_path: str | Path,
    output_path: str | Path,
    *,
    expected_policy_fingerprint: str,
    max_age_seconds: int = 86400,
    now: datetime | None = None,
) -> RenderResult:
    """Render a startup config, falling back to static routes for catalog errors."""

    if max_age_seconds < 0:
        raise RuntimeConfigError("catalog maximum age cannot be negative")

    base_path = Path(base_config_path)
    destination = Path(output_path)
    if _same_path(base_path, destination):
        raise RuntimeConfigError("runtime config must not overwrite the static config")

    document = _load_base_config(base_path)
    static_models = document["model_list"]
    artifact_path = Path(catalog_path)
    state: CatalogState = "missing"
    detail = f"approved catalog not found at {artifact_path}"
    routes: list[dict[str, Any]] = []

    try:
        artifact = _load_catalog(artifact_path)
        routes = _routes_from_catalog(
            artifact,
            static_models=static_models,
            expected_policy_fingerprint=expected_policy_fingerprint,
            max_age_seconds=max_age_seconds,
            now=now,
        )
        state = "fresh"
        detail = f"loaded {len(routes)} approved models"
    except FileNotFoundError:
        pass
    except StaleCatalogArtifactError as exc:
        state = "stale"
        detail = str(exc)
    except (CatalogArtifactError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        state = "invalid"
        detail = f"approved catalog rejected: {exc}"

    document["model_list"] = [*static_models, *routes]
    _atomic_yaml(destination, document)
    return RenderResult(
        output_path=destination,
        catalog_state=state,
        added_models=len(routes),
        detail=detail,
    )


def _load_base_config(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"cannot read static LiteLLM config {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeConfigError("static LiteLLM config must be a mapping")
    document = dict(value)
    model_list = document.get("model_list")
    if not isinstance(model_list, list) or not all(isinstance(item, Mapping) for item in model_list):
        raise RuntimeConfigError("static LiteLLM model_list must be a list of mappings")
    document["model_list"] = [dict(item) for item in model_list]
    return document


def _load_catalog(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CatalogArtifactError("top level must be an object")
    return value


def _routes_from_catalog(
    artifact: Mapping[str, Any],
    *,
    static_models: list[dict[str, Any]],
    expected_policy_fingerprint: str,
    max_age_seconds: int,
    now: datetime | None,
) -> list[dict[str, Any]]:
    if artifact.get("schema_version") != 1:
        raise CatalogArtifactError("unsupported or missing schema_version")
    if artifact.get("policy_fingerprint") != expected_policy_fingerprint:
        raise CatalogArtifactError("catalog policy fingerprint does not match the active policy")
    generated_at = _generated_at(artifact.get("generated_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise RuntimeConfigError("the renderer clock must be timezone-aware")
    age = (current.astimezone(timezone.utc) - generated_at).total_seconds()
    if age < -_MAX_FUTURE_SKEW_SECONDS:
        raise CatalogArtifactError("generated_at is in the future")
    stale = artifact.get("stale")
    if not isinstance(stale, bool):
        raise CatalogArtifactError("stale must be a boolean")
    if stale or age > max_age_seconds:
        raise StaleCatalogArtifactError(
            f"approved catalog is stale ({max(age, 0):.0f}s old; maximum {max_age_seconds}s)"
        )

    models = artifact.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogArtifactError("models must be a non-empty list")

    static_names = {item.get("model_name") for item in static_models if isinstance(item.get("model_name"), str)}
    seen: set[str] = set()
    routes: list[dict[str, Any]] = []
    for index, value in enumerate(models):
        if not isinstance(value, Mapping):
            raise CatalogArtifactError(f"models[{index}] must be an object")
        model_id = value.get("id")
        if (
            not isinstance(model_id, str)
            or len(model_id) > 256
            or not _OPENROUTER_MODEL_ID.fullmatch(model_id)
            or any(character in model_id for character in (*_GLOB_META, ","))
        ):
            raise CatalogArtifactError(f"models[{index}].id is not a raw OpenRouter model ID")
        if model_id in seen:
            raise CatalogArtifactError(f"duplicate model ID: {model_id}")
        seen.add(model_id)

        public_name = f"openrouter/{model_id}"
        if public_name in static_names:
            raise CatalogArtifactError(f"catalog route collides with static route: {public_name}")

        pricing = value.get("pricing_usd_per_million")
        if not isinstance(pricing, Mapping):
            raise CatalogArtifactError(f"models[{index}].pricing_usd_per_million must be an object")
        input_cost = _per_token_price(pricing.get("prompt"), f"models[{index}].pricing.prompt")
        output_cost = _per_token_price(pricing.get("completion"), f"models[{index}].pricing.completion")
        routes.append(
            {
                "model_name": public_name,
                "litellm_params": {
                    "model": public_name,
                    "api_base": _OPENROUTER_API_BASE,
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                },
                "model_info": {
                    "input_cost_per_token": input_cost,
                    "output_cost_per_token": output_cost,
                },
            }
        )

    routes.sort(key=lambda item: str(item["model_name"]))
    return routes


def _generated_at(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise CatalogArtifactError("generated_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogArtifactError("generated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CatalogArtifactError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _per_token_price(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CatalogArtifactError(f"{field} must be a numeric USD-per-million value")
    price = float(value)
    if not math.isfinite(price) or price < 0:
        raise CatalogArtifactError(f"{field} must be finite and non-negative")
    per_token = price / _PER_MILLION
    if price > 0 and per_token == 0:
        raise CatalogArtifactError(f"{field} is too small to represent per token")
    return per_token


def _atomic_yaml(path: Path, document: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=True)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise RuntimeConfigError(f"cannot write LiteLLM runtime config {path}: {exc}") from exc


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)
