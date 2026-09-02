"""Client-credentials service example; no provider key is used."""

from __future__ import annotations

import os

import httpx
from openai import OpenAI


def service_token() -> str:
    token_fields = {
        "grant_type": "client_credentials",
        "client_id": os.environ["OIDC_CLIENT_ID"],
        "client_secret": os.environ["OIDC_CLIENT_SECRET"],
    }
    if requested_scope := os.getenv("OIDC_SCOPE", "").strip():
        token_fields["scope"] = requested_scope

    response = httpx.post(
        os.environ["OIDC_TOKEN_URL"],
        data=token_fields,
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


client = OpenAI(base_url=os.environ["GATEWAY_URL"], api_key=service_token())
response = client.chat.completions.create(
    model=os.getenv("MODEL", "general-fast"),
    messages=[{"role": "user", "content": "Summarize the deployment status in one sentence."}],
)
print(f"model={response.model} content={response.choices[0].message.content}")
