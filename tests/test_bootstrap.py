from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from gateway.bootstrap.bootstrap import main


@respx.mock
def test_bootstrap_clears_team_router_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        """
teams:
  automation:
    monthly_budget_usd: 10
    rpm_limit: 20
    tpm_limit: 3000
    models:
      allow: [general-fast, general-quality]
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_INTERNAL_URL", "https://gateway.internal")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("OIDC_POLICY_FILE", str(policy))
    respx.get("https://gateway.internal/team/info").mock(return_value=httpx.Response(200, json={}))
    update = respx.post("https://gateway.internal/team/update").mock(return_value=httpx.Response(200, json={}))

    assert main() == 0

    assert update.called
    payload = json.loads(update.calls.last.request.content)
    assert payload["router_settings"] == {}
    assert payload["model_aliases"] == {}
    assert payload["models"] == ["general-fast", "general-quality"]
