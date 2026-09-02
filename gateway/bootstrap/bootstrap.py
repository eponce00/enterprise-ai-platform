"""Synchronize configured teams through LiteLLM's supported management API."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


def main() -> int:
    base_url = os.environ["LITELLM_INTERNAL_URL"].rstrip("/")
    master_key = os.environ["LITELLM_MASTER_KEY"]
    policy_path = Path(os.getenv("OIDC_POLICY_FILE", "/app/policy.yaml"))
    document = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    headers = {"Authorization": f"Bearer {master_key}"}

    with httpx.Client(base_url=base_url, headers=headers, timeout=20.0) as client:
        for team_id, team in document["teams"].items():
            payload: dict[str, Any] = {
                "team_id": team_id,
                "team_alias": team_id,
                "models": team.get("models", {}).get("allow", []),
                "max_budget": team.get("monthly_budget_usd"),
                "budget_duration": "1mo",
                "rpm_limit": team.get("rpm_limit"),
                "tpm_limit": team.get("tpm_limit"),
                "metadata": {"managed_by": "enterprise-ai-platform"},
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            exists = client.get("/team/info", params={"team_id": team_id})
            endpoint = "/team/update" if exists.status_code == 200 else "/team/new"
            response = client.post(endpoint, json=payload)
            if response.status_code >= 400:
                print(f"failed to synchronize team {team_id}: HTTP {response.status_code}", file=sys.stderr)
                return 1
            print(f"synchronized team {team_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
