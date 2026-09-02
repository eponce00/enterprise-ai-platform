"""Synchronize and filter OpenRouter's model API without hard-coded IDs."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml


class CatalogSyncError(RuntimeError):
    pass


_GLOB_META = frozenset("*?[]")
_PUBLIC_CATALOG_MODE = 0o644


@dataclass(frozen=True)
class CatalogPolicy:
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()
    require_zdr: bool = True
    require_tools: bool = False
    min_context_tokens: int = 0
    max_prompt_usd_per_million: float | None = None
    max_completion_usd_per_million: float | None = None
    required_input_modalities: tuple[str, ...] = ("text",)
    max_stale_seconds: int = 86400

    @classmethod
    def from_file(cls, path: str | Path) -> CatalogPolicy:
        try:
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise CatalogSyncError(f"cannot read catalog policy: {exc}") from exc
        if not isinstance(document, Mapping):
            raise CatalogSyncError("catalog policy root must be a mapping")
        values = document.get("catalog", document)
        if not isinstance(values, Mapping):
            raise CatalogSyncError("catalog policy must be a mapping")
        try:
            return cls(
                allow=_string_tuple(values, "allow", default=("*",)),
                deny=_string_tuple(values, "deny"),
                require_zdr=_policy_bool(values, "require_zdr", default=True),
                require_tools=_policy_bool(values, "require_tools", default=False),
                min_context_tokens=_policy_nonnegative_int(values, "min_context_tokens", default=0),
                max_prompt_usd_per_million=_optional_float(values.get("max_prompt_usd_per_million")),
                max_completion_usd_per_million=_optional_float(values.get("max_completion_usd_per_million")),
                required_input_modalities=_string_tuple(values, "required_input_modalities", default=("text",)),
                max_stale_seconds=_policy_nonnegative_int(values, "max_stale_seconds", default=86400),
            )
        except CatalogSyncError:
            raise
        except (TypeError, ValueError) as exc:
            raise CatalogSyncError("catalog policy contains an invalid value") from exc


@dataclass(frozen=True)
class CatalogSyncResult:
    models: list[dict[str, Any]]
    stale: bool
    source: str
    rejected: dict[str, int] = field(default_factory=dict)


class OpenRouterCatalogClient:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key
        self._client = client

    def fetch(self, require_zdr: bool = True) -> list[dict[str, Any]]:
        # The authenticated endpoint reflects account/guardrail/provider policy.
        endpoint = "/api/v1/models/user" if self.api_key else "/api/v1/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        params = {"zdr": "true"} if require_zdr and not self.api_key else {}
        close_client = self._client is None
        client = self._client or httpx.Client(base_url="https://openrouter.ai", timeout=30.0)
        try:
            response = client.get(endpoint, headers=headers, params=params)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise CatalogSyncError("OpenRouter returned invalid catalog JSON") from exc
            if not isinstance(payload, Mapping):
                raise CatalogSyncError("OpenRouter returned an invalid model catalog")
            data = payload.get("data")
            if not isinstance(data, list):
                raise CatalogSyncError("OpenRouter returned an invalid model catalog")
            return [dict(item) for item in data if isinstance(item, Mapping)]
        except httpx.HTTPError as exc:
            raise CatalogSyncError("OpenRouter model catalog request failed") from exc
        finally:
            if close_client:
                client.close()


def filter_models(models: list[dict[str, Any]], policy: CatalogPolicy) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for model in models:
        reason = _rejection_reason(model, policy)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        accepted.append(_normalize(model))
    accepted.sort(key=lambda item: item["id"])
    return accepted, rejected


def sync_catalog(
    policy: CatalogPolicy,
    output: str | Path,
    *,
    client: OpenRouterCatalogClient | None = None,
    now: float | None = None,
) -> CatalogSyncResult:
    output_path = Path(output)
    authenticated = (client and client.api_key) or os.getenv("OPENROUTER_API_KEY")
    source = "/api/v1/models/user" if authenticated else "/api/v1/models"
    client = client or OpenRouterCatalogClient(os.getenv("OPENROUTER_API_KEY") or None)
    now = time.time() if now is None else now
    try:
        raw = client.fetch(require_zdr=policy.require_zdr)
        models, rejected = filter_models(raw, policy)
        if not models:
            raise CatalogSyncError("catalog policy rejected every model")
        payload = {
            "schema_version": 1,
            "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "policy_fingerprint": policy_fingerprint(policy),
            "source": source,
            "stale": False,
            "models": models,
            "rejected": rejected,
        }
        _atomic_json(output_path, payload)
        return CatalogSyncResult(models=models, stale=False, source=source, rejected=rejected)
    except CatalogSyncError:
        cached = _load_fresh_cache(
            output_path,
            now,
            policy.max_stale_seconds,
            policy_fingerprint(policy),
        )
        if cached is None:
            raise
        return CatalogSyncResult(
            models=list(cached.get("models") or []),
            stale=True,
            source=str(cached.get("source") or "cache"),
            rejected=dict(cached.get("rejected") or {}),
        )


def _rejection_reason(model: Mapping[str, Any], policy: CatalogPolicy) -> str | None:
    model_id = model.get("id")
    if (
        not isinstance(model_id, str)
        or not model_id
        or any(character.isspace() or character in _GLOB_META for character in model_id)
    ):
        return "invalid"
    if not any(fnmatch.fnmatchcase(model_id, pattern) for pattern in policy.allow):
        return "not_allowed"
    if any(fnmatch.fnmatchcase(model_id, pattern) for pattern in policy.deny):
        return "denied"
    context_length = _context_length(model.get("context_length"))
    if context_length is None:
        return "invalid"
    if context_length < policy.min_context_tokens:
        return "context"
    supported_value = model.get("supported_parameters") or []
    if not isinstance(supported_value, list) or not all(isinstance(item, str) for item in supported_value):
        return "invalid"
    supported = set(supported_value)
    if policy.require_tools and "tools" not in supported:
        return "tools"
    architecture = model.get("architecture")
    if not isinstance(architecture, Mapping):
        return "invalid"
    input_modalities = architecture.get("input_modalities") or ["text"]
    output_modalities = architecture.get("output_modalities") or ["text"]
    if not isinstance(input_modalities, list) or not all(isinstance(item, str) for item in input_modalities):
        return "invalid"
    if not isinstance(output_modalities, list) or not all(isinstance(item, str) for item in output_modalities):
        return "invalid"
    modalities = set(input_modalities)
    if not set(policy.required_input_modalities).issubset(modalities):
        return "modality"
    pricing = model.get("pricing")
    if not isinstance(pricing, Mapping):
        return "invalid"
    prompt = _per_million(pricing.get("prompt"))
    completion = _per_million(pricing.get("completion"))
    if prompt is None or completion is None:
        return "invalid"
    if policy.max_prompt_usd_per_million is not None and prompt > policy.max_prompt_usd_per_million:
        return "prompt_price"
    if policy.max_completion_usd_per_million is not None and completion > policy.max_completion_usd_per_million:
        return "completion_price"
    expiration = model.get("expiration_date")
    if expiration is not None and not isinstance(expiration, str):
        return "invalid"
    if expiration:
        try:
            expires_at = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return "expired"
        except ValueError:
            return "invalid"
    return None


def _normalize(model: Mapping[str, Any]) -> dict[str, Any]:
    pricing_value = model.get("pricing")
    pricing = pricing_value if isinstance(pricing_value, Mapping) else {}
    architecture_value = model.get("architecture")
    architecture = architecture_value if isinstance(architecture_value, Mapping) else {}
    context_length = _context_length(model.get("context_length"))
    return {
        "id": model["id"],
        "canonical_slug": model.get("canonical_slug") or model["id"],
        "name": model.get("name") or model["id"],
        "description": model.get("description") or "",
        "context_length": context_length if context_length is not None else 0,
        "supported_parameters": sorted(set(model.get("supported_parameters") or [])),
        "input_modalities": architecture.get("input_modalities") or ["text"],
        "output_modalities": architecture.get("output_modalities") or ["text"],
        "pricing_usd_per_million": {
            "prompt": _normalized_price(pricing.get("prompt")),
            "completion": _normalized_price(pricing.get("completion")),
        },
    }


def _per_million(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        price = float(value) * 1_000_000
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price >= 0 else None


def _normalized_price(value: Any) -> float | None:
    return _per_million(value)


def _context_length(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 and str(parsed) == value.strip() else None
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CatalogSyncError("catalog price ceiling must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalogSyncError("catalog price ceiling must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise CatalogSyncError("catalog price ceiling must be finite and non-negative")
    return parsed


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, _PUBLIC_CATALOG_MODE)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_fresh_cache(
    path: Path,
    now: float,
    max_stale_seconds: int,
    policy_fingerprint: str,
) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("stale") is not False
            or value.get("policy_fingerprint") != policy_fingerprint
        ):
            return None
        generated_at = value.get("generated_at")
        if not isinstance(generated_at, str):
            return None
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            return None
        age = now - generated.timestamp()
        if age < -300 or age > max_stale_seconds:
            return None
        models = value.get("models")
        if not isinstance(models, list) or not models or not all(_valid_cached_model(item) for item in models):
            return None
        return value
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError, OverflowError):
        return None


def policy_fingerprint(policy: CatalogPolicy) -> str:
    document = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _string_tuple(
    values: Mapping[str, Any],
    field_name: str,
    *,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = values.get(field_name)
    if value is None:
        return default
    if isinstance(value, str):
        items = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = tuple(value)
    else:
        raise CatalogSyncError(f"catalog {field_name} must be a string or a list of strings")
    normalized = tuple(item.strip() for item in items)
    if any(not item for item in normalized):
        raise CatalogSyncError(f"catalog {field_name} cannot contain an empty value")
    return normalized


def _policy_bool(values: Mapping[str, Any], field_name: str, *, default: bool) -> bool:
    value = values.get(field_name, default)
    if not isinstance(value, bool):
        raise CatalogSyncError(f"catalog {field_name} must be a boolean")
    return value


def _policy_nonnegative_int(values: Mapping[str, Any], field_name: str, *, default: int) -> int:
    value = values.get(field_name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogSyncError(f"catalog {field_name} must be a non-negative integer")
    return int(value)


def _valid_cached_model(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    model_id = value.get("id")
    if not isinstance(model_id, str) or not model_id:
        return False
    pricing = value.get("pricing_usd_per_million")
    if not isinstance(pricing, Mapping):
        return False
    return all(
        isinstance(price, int | float) and not isinstance(price, bool) and math.isfinite(price) and price >= 0
        for price in (pricing.get("prompt"), pricing.get("completion"))
    )
