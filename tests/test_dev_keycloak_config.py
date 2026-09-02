"""Checks for the development realm assumptions used by integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _clients_by_id() -> dict[str, dict[str, Any]]:
    realm_path = Path(__file__).parents[1] / "gateway" / "dev" / "keycloak" / "realm.json"
    realm = json.loads(realm_path.read_text(encoding="utf-8"))
    return {client["clientId"]: client for client in realm["clients"]}


def test_development_clients_emit_oidc_subject_claims() -> None:
    clients = _clients_by_id()

    assert "basic" in clients["enterprise-ai-cli"]["defaultClientScopes"]
    assert "offline_access" in clients["enterprise-ai-cli"]["optionalClientScopes"]
    assert "basic" in clients["example-service"]["defaultClientScopes"]


def test_development_user_can_receive_refresh_tokens() -> None:
    realm_path = Path(__file__).parents[1] / "gateway" / "dev" / "keycloak" / "realm.json"
    realm = json.loads(realm_path.read_text(encoding="utf-8"))
    users = {user["username"]: user for user in realm["users"]}

    assert "offline_access" in users["developer"]["realmRoles"]
