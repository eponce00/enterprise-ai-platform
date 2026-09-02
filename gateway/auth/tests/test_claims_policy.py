from __future__ import annotations

import pytest

from gateway.auth.claims import Identity, extract_identity
from gateway.auth.policy import PolicyDenied, PolicyEngine


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(
        {
            "identity": {"organization": "organization"},
            "mappings": [
                {"client_id": "batch", "kind": "service", "team": "automation"},
                {"oidc_group": "developers", "kind": "human", "team": "developers"},
            ],
            "teams": {
                "developers": {
                    "monthly_budget_usd": 10,
                    "rpm_limit": 2,
                    "models": {"allow": ["general-*", "openrouter/*"], "deny": ["openrouter/blocked/*"]},
                },
                "automation": {"models": {"allow": ["cheap-batch"]}},
            },
            "privacy_profiles": {
                "default": {
                    "zdr": True,
                    "data_collection": "deny",
                    "provider_allowlist": ["approved"],
                    "provider_denylist": ["denied"],
                }
            },
        }
    )


def human() -> Identity:
    return Identity(
        subject="user",
        issuer="https://idp.example",
        email="user@example.com",
        groups=frozenset({"developers"}),
        roles=frozenset(),
        client_id="cli",
        kind="human",
    )


def test_human_group_maps_to_team(engine: PolicyEngine) -> None:
    assert engine.resolve(human()).team == "developers"


def test_service_identity_is_distinct(engine: PolicyEngine) -> None:
    identity = extract_identity(
        {"iss": "https://idp.example", "sub": "svc", "azp": "batch", "identity_type": "service"}
    )
    assert identity.kind == "service"
    assert engine.resolve(identity).team == "automation"


def test_unknown_group_fails_closed(engine: PolicyEngine) -> None:
    identity = Identity(**{**human().__dict__, "groups": frozenset({"unknown"})})
    with pytest.raises(PolicyDenied, match="no authorized"):
        engine.resolve(identity)


def test_model_allow_and_deny(engine: PolicyEngine) -> None:
    decision = engine.resolve(human())
    engine.authorize_model(decision, "general-fast")
    with pytest.raises(PolicyDenied, match="denied"):
        engine.authorize_model(decision, "openrouter/blocked/model")
    with pytest.raises(PolicyDenied, match="not allowed"):
        engine.authorize_model(decision, "direct/unapproved")


def test_budget_and_rate_enforcement(engine: PolicyEngine) -> None:
    decision = engine.resolve(human())
    with pytest.raises(PolicyDenied, match="budget"):
        engine.authorize_model(decision, "general-fast", team_spend_usd=10)
    with pytest.raises(PolicyDenied, match="rate"):
        engine.authorize_model(decision, "general-fast", observed_rpm=2)


def test_provider_restrictions(engine: PolicyEngine) -> None:
    decision = engine.resolve(human())
    engine.authorize_model(decision, "general-fast", provider="approved")
    with pytest.raises(PolicyDenied, match="not approved"):
        engine.authorize_model(decision, "general-fast", provider="other")
    with pytest.raises(PolicyDenied, match="denied"):
        engine.authorize_model(decision, "general-fast", provider="denied")
