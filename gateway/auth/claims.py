"""Vendor-neutral extraction of identities from validated OIDC claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


def claim_at(claims: Mapping[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def string_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(item for item in value.replace(",", " ").split() if item)
    if isinstance(value, list | tuple | set):
        return frozenset(str(item) for item in value if str(item))
    return frozenset()


@dataclass(frozen=True)
class Identity:
    subject: str
    issuer: str
    email: str | None
    groups: frozenset[str]
    roles: frozenset[str]
    client_id: str | None
    kind: Literal["human", "service"]
    display_name: str | None = None

    @property
    def stable_id(self) -> str:
        # Issuer is included so subjects from separate trusted issuers cannot collide.
        return f"{self.issuer}|{self.subject}"


def extract_identity(
    claims: Mapping[str, Any],
    *,
    group_claim: str = "groups",
    role_claim: str = "roles",
    service_claim: str = "identity_type",
    service_values: frozenset[str] = frozenset({"service", "workload"}),
) -> Identity:
    subject = claims.get("sub")
    issuer = claims.get("iss")
    if not isinstance(subject, str) or not subject:
        raise ValueError("subject claim is required")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("issuer claim is required")

    email = claims.get("email")
    client_id = claims.get("azp") or claims.get("client_id")
    explicit_type = claim_at(claims, service_claim)
    grant_type = claims.get("gty") or claims.get("grant_type")
    is_service = str(explicit_type).lower() in service_values or grant_type == "client-credentials"
    # Many IdPs omit an identity-type claim for client-credentials tokens. An
    # absent email plus a client identifier is a conservative secondary signal;
    # policy mappings can still explicitly require kind=service.
    if email is None and client_id and claims.get("preferred_username") == f"service-account-{client_id}":
        is_service = True

    return Identity(
        subject=subject,
        issuer=issuer.rstrip("/"),
        email=email if isinstance(email, str) else None,
        groups=string_set(claim_at(claims, group_claim)),
        roles=string_set(claim_at(claims, role_claim)),
        client_id=str(client_id) if client_id is not None else None,
        kind="service" if is_service else "human",
        display_name=_first_string(claims, "name", "preferred_username"),
    )


def _first_string(claims: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return None
