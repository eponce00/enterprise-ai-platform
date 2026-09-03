"""Container entrypoint that renders catalog routes before starting LiteLLM."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from catalog.sync import CatalogPolicy, CatalogSyncError, policy_fingerprint
from gateway.auth.oidc_auth import RuntimeModelPolicyError, validate_runtime_model_policy
from gateway.auth.policy import PolicyEngine, PolicyError
from gateway.auth.settings import GatewaySettings, OIDCSettings, SettingsError
from gateway.catalog_config import RuntimeConfigError, render_runtime_config

DEFAULT_BASE_CONFIG = "/app/gateway/litellm/config.yaml"
DEFAULT_CATALOG = "/app/catalog/generated/approved-models.json"
DEFAULT_CATALOG_POLICY = "/app/catalog-policy.yaml"
# The container has one non-root application process and this file is replaced
# atomically before LiteLLM starts; no sensitive values are added by the renderer.
DEFAULT_RUNTIME_CONFIG = "/tmp/enterprise-ai-litellm.yaml"  # noqa: S108
DEFAULT_UPSTREAM_ENTRYPOINT = "/app/docker/prod_entrypoint.sh"
_ALTERNATE_CONFIG_SOURCE_ENV = frozenset(
    {
        "CONFIG_FILE_PATH",
        "LITELLM_CONFIG_BUCKET_NAME",
        "LITELLM_CONFIG_BUCKET_OBJECT_KEY",
        "LITELLM_CONFIG_BUCKET_TYPE",
        "WORKER_CONFIG",
    }
)
_UNRESOLVED_ENV_REFERENCE = re.compile(
    r"(?:"
    r"\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*(?:(?::?[-+?=])[^{}]*)?\}"
    r"|os\.environ/[A-Za-z_][A-Za-z0-9_]*"
    r")"
)


def prepare_litellm_args(
    arguments: Sequence[str],
    *,
    runtime_config: str | Path,
    default_base_config: str | Path,
) -> tuple[Path, list[str]]:
    """Find LiteLLM's static config argument and point it at the rendered copy."""

    rewritten = list(arguments)
    matches: list[tuple[int, str]] = []
    for index, argument in enumerate(rewritten):
        if argument == "--config":
            if index + 1 >= len(rewritten) or rewritten[index + 1].startswith("--"):
                raise RuntimeConfigError("--config requires a path")
            matches.append((index, rewritten[index + 1]))
        elif argument.startswith("--config="):
            matches.append((index, argument.partition("=")[2]))

    if len(matches) > 1:
        raise RuntimeConfigError("multiple --config arguments are not supported")

    runtime = str(runtime_config)
    if not matches:
        base = Path(default_base_config)
        rewritten.extend(["--config", runtime])
        return base, rewritten

    index, configured = matches[0]
    if not configured:
        raise RuntimeConfigError("--config requires a path")
    if rewritten[index] == "--config":
        rewritten[index + 1] = runtime
    else:
        rewritten[index] = f"--config={runtime}"
    return Path(configured), rewritten


def reject_alternate_config_sources() -> None:
    """Ensure upstream cannot replace the runtime document after validation."""

    configured = sorted(name for name in _ALTERNATE_CONFIG_SOURCE_ENV if name in os.environ)
    if configured:
        raise RuntimeConfigError(
            "alternate LiteLLM config-source environment variables are not allowed: " + ", ".join(configured)
        )


def validate_auth_configuration() -> None:
    """Validate local OIDC and policy configuration before reporting healthy."""

    for name in ("DATABASE_URL", "LITELLM_MASTER_KEY"):
        value = os.getenv(name)
        if value is None or not value.strip() or value != value.strip():
            raise SettingsError(f"{name} must be set to a non-empty, trimmed value")
        # DATABASE_URL can embed a forgotten password reference, and an opaque
        # admin secret should never contain an unexpanded shell/Compose token.
        if _UNRESOLVED_ENV_REFERENCE.search(value):
            raise SettingsError(f"{name} must be resolved before startup")
    OIDCSettings.from_env()
    gateway = GatewaySettings.from_env()
    PolicyEngine.from_file(gateway.policy_file)


def main(arguments: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if arguments is None else arguments)
    runtime_path = Path(os.getenv("LITELLM_RUNTIME_CONFIG", DEFAULT_RUNTIME_CONFIG))
    try:
        max_age_seconds = int(os.getenv("APPROVED_CATALOG_MAX_AGE_SECONDS", "86400"))
    except ValueError as exc:
        raise SystemExit("APPROVED_CATALOG_MAX_AGE_SECONDS must be an integer") from exc

    try:
        reject_alternate_config_sources()
        base_path, litellm_args = prepare_litellm_args(
            args,
            runtime_config=runtime_path,
            default_base_config=os.getenv("LITELLM_BASE_CONFIG", DEFAULT_BASE_CONFIG),
        )
        catalog_policy = CatalogPolicy.from_file(os.getenv("CATALOG_POLICY_FILE", DEFAULT_CATALOG_POLICY))
        result = render_runtime_config(
            base_path,
            os.getenv("APPROVED_CATALOG_PATH", DEFAULT_CATALOG),
            runtime_path,
            expected_policy_fingerprint=policy_fingerprint(catalog_policy),
            max_age_seconds=max_age_seconds,
        )
        validate_auth_configuration()
        validate_runtime_model_policy()
    except (CatalogSyncError, PolicyError, RuntimeConfigError, RuntimeModelPolicyError, SettingsError) as exc:
        raise SystemExit(f"gateway startup configuration failed: {exc}") from exc

    level = "info" if result.catalog_state in {"fresh", "missing"} else "warning"
    print(
        f"gateway catalog {level}: {result.detail}; starting with {result.added_models} explicit approved routes",
        file=sys.stderr,
        flush=True,
    )

    upstream = os.getenv("LITELLM_UPSTREAM_ENTRYPOINT", DEFAULT_UPSTREAM_ENTRYPOINT)
    executable = [upstream, *litellm_args]
    if os.path.dirname(upstream):
        os.execv(upstream, executable)  # noqa: S606 - replacing PID 1 is intentional
    os.execvp(upstream, executable)  # noqa: S606 - replacing PID 1 is intentional


if __name__ == "__main__":
    main()
