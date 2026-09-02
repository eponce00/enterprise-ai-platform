"""Fail-closed identity mapping and request policy evaluation."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .claims import Identity


class PolicyError(ValueError):
    """Policy configuration is invalid."""


class PolicyDenied(PermissionError):
    """An authenticated identity or request is not authorized."""


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise PolicyError("policy string list has an invalid value")


@dataclass(frozen=True)
class PolicyDecision:
    organization: str
    team: str
    profile: str
    allowed_models: tuple[str, ...]
    denied_models: tuple[str, ...]
    monthly_budget_usd: float | None
    per_user_monthly_budget_usd: float | None
    rpm_limit: int | None
    tpm_limit: int | None
    privacy: Mapping[str, Any] = field(default_factory=dict)


class PolicyEngine:
    def __init__(self, document: Mapping[str, Any]):
        self.document = document
        identity_config = document.get("identity") or {}
        mappings = document.get("mappings") or []
        teams = document.get("teams") or {}
        privacy_profiles = document.get("privacy_profiles") or {}
        if not isinstance(identity_config, Mapping):
            raise PolicyError("identity policy must be a mapping")
        if not isinstance(mappings, list):
            raise PolicyError("identity mappings must be a list")
        if not isinstance(teams, Mapping):
            raise PolicyError("teams policy must be a mapping")
        if not isinstance(privacy_profiles, Mapping):
            raise PolicyError("privacy profiles must be a mapping")
        self.identity_config = dict(identity_config)
        self.mappings = list(mappings)
        self.teams = dict(teams)
        self.privacy_profiles = dict(privacy_profiles)
        if not self.mappings:
            raise PolicyError("at least one identity mapping is required")
        if not self.teams:
            raise PolicyError("at least one team policy is required")
        for name, profile in self.privacy_profiles.items():
            _validate_privacy_profile(name, profile)

    @classmethod
    def from_file(cls, path: str | Path) -> PolicyEngine:
        policy_path = Path(path)
        try:
            with policy_path.open("r", encoding="utf-8") as handle:
                document = yaml.safe_load(handle) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PolicyError(f"cannot load policy file: {policy_path}") from exc
        if not isinstance(document, Mapping):
            raise PolicyError("policy root must be a mapping")
        return cls(document)

    @property
    def group_claim(self) -> str:
        return str(self.identity_config.get("group_claim", "groups"))

    @property
    def role_claim(self) -> str:
        return str(self.identity_config.get("role_claim", "roles"))

    @property
    def service_claim(self) -> str:
        return str(self.identity_config.get("service_claim", "identity_type"))

    @property
    def service_values(self) -> frozenset[str]:
        configured = self.identity_config.get("service_values", ["service", "workload"])
        return frozenset(item.lower() for item in _list(configured))

    def resolve(self, identity: Identity) -> PolicyDecision:
        mapping = next((item for item in self.mappings if self._mapping_matches(item, identity)), None)
        if mapping is None:
            raise PolicyDenied("identity has no authorized group, role, or client mapping")

        team_name = mapping.get("team")
        if not isinstance(team_name, str) or team_name not in self.teams:
            raise PolicyError("identity mapping references an unknown team")
        team = self.teams[team_name]
        if not isinstance(team, Mapping):
            raise PolicyError("team policy must be a mapping")
        profile_name = str(mapping.get("privacy_profile") or team.get("privacy_profile") or "default")
        privacy = self.privacy_profiles.get(profile_name)
        if not isinstance(privacy, Mapping):
            raise PolicyError(f"unknown privacy profile: {profile_name}")
        models = team.get("models") or {}
        if not isinstance(models, Mapping):
            raise PolicyError("team models policy must be a mapping")

        return PolicyDecision(
            organization=str(self.identity_config.get("organization", "organization")),
            team=team_name,
            profile=profile_name,
            allowed_models=tuple(_list(models.get("allow"))),
            denied_models=tuple(_list(models.get("deny"))),
            monthly_budget_usd=_optional_number(team.get("monthly_budget_usd")),
            per_user_monthly_budget_usd=_optional_number(team.get("per_user_monthly_budget_usd")),
            rpm_limit=_optional_int(team.get("rpm_limit")),
            tpm_limit=_optional_int(team.get("tpm_limit")),
            privacy=dict(privacy),
        )

    def authorize_model(
        self,
        decision: PolicyDecision,
        model: str,
        *,
        team_spend_usd: float = 0.0,
        user_spend_usd: float = 0.0,
        observed_rpm: int = 0,
        provider: str | None = None,
    ) -> None:
        if not model:
            raise PolicyDenied("request does not specify a model")
        if any(fnmatch.fnmatchcase(model, pattern) for pattern in decision.denied_models):
            raise PolicyDenied(f"model is denied for team {decision.team}")
        if not decision.allowed_models or not any(
            fnmatch.fnmatchcase(model, pattern) for pattern in decision.allowed_models
        ):
            raise PolicyDenied(f"model is not allowed for team {decision.team}")
        if decision.monthly_budget_usd is not None and team_spend_usd >= decision.monthly_budget_usd:
            raise PolicyDenied(f"monthly budget is exhausted for team {decision.team}")
        if decision.per_user_monthly_budget_usd is not None and user_spend_usd >= decision.per_user_monthly_budget_usd:
            raise PolicyDenied("monthly user budget is exhausted")
        if decision.rpm_limit is not None and observed_rpm >= decision.rpm_limit:
            raise PolicyDenied("request rate limit is exhausted")
        allowlist = _list(decision.privacy.get("provider_allowlist"))
        denylist = _list(decision.privacy.get("provider_denylist"))
        if provider and provider in denylist:
            raise PolicyDenied("provider is denied by the privacy profile")
        if provider and allowlist and provider not in allowlist:
            raise PolicyDenied("provider is not approved by the privacy profile")

    @staticmethod
    def _mapping_matches(mapping: Any, identity: Identity) -> bool:
        if not isinstance(mapping, Mapping):
            raise PolicyError("identity mapping must be a mapping")
        kind = mapping.get("kind")
        if kind is not None and kind != identity.kind:
            return False
        conditions = []
        if "oidc_group" in mapping:
            conditions.append(str(mapping["oidc_group"]) in identity.groups)
        if "oidc_role" in mapping:
            conditions.append(str(mapping["oidc_role"]) in identity.roles)
        if "client_id" in mapping:
            conditions.append(str(mapping["client_id"]) == identity.client_id)
        # A mapping with no selector would accidentally authorize everyone.
        return bool(conditions) and all(conditions)


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise PolicyError("budget must be a non-negative number")
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError("rate limit must be a positive integer")
    return int(value)


def _validate_privacy_profile(name: object, value: object) -> None:
    if not isinstance(name, str) or not name.strip() or not isinstance(value, Mapping):
        raise PolicyError("privacy profile names and values must be valid mappings")
    supported = {
        "zdr",
        "require_zdr",
        "data_collection",
        "deny_data_collection",
        "require_parameters",
        "provider_allowlist",
        "provider_denylist",
    }
    unknown = set(value).difference(supported)
    if unknown:
        raise PolicyError(f"privacy profile {name!r} has unsupported fields: {', '.join(sorted(map(str, unknown)))}")
    for field_name in ("zdr", "require_zdr", "deny_data_collection", "require_parameters"):
        if field_name in value and not isinstance(value[field_name], bool):
            raise PolicyError(f"privacy profile {name!r} field {field_name} must be boolean")
    data_collection = value.get("data_collection")
    if data_collection is not None and (
        not isinstance(data_collection, str) or data_collection not in {"allow", "deny"}
    ):
        raise PolicyError(f"privacy profile {name!r} data_collection must be 'allow' or 'deny'")
    if "zdr" in value and "require_zdr" in value and value["zdr"] != value["require_zdr"]:
        raise PolicyError(f"privacy profile {name!r} has conflicting ZDR fields")
    allowed = _list(value.get("provider_allowlist"))
    denied = _list(value.get("provider_denylist"))
    if any(not item.strip() or item != item.strip() for item in (*allowed, *denied)):
        raise PolicyError(f"privacy profile {name!r} provider names must be non-empty and trimmed")
    overlap = set(allowed).intersection(denied)
    if overlap:
        raise PolicyError(f"privacy profile {name!r} allows and denies the same provider")
