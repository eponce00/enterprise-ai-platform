from __future__ import annotations

import os
import time
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
import pytest
from openai import OpenAI


def _local_token() -> str:
    issuer = os.getenv("E2E_OIDC_ISSUER", "http://127.0.0.1:8080/realms/enterprise-ai")
    response = httpx.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("E2E_CLIENT_ID", "example-service"),
            "client_secret": os.getenv("E2E_CLIENT_SECRET", "development-only-service-secret"),
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _human_token() -> str:
    issuer = os.getenv("E2E_OIDC_ISSUER", "http://127.0.0.1:8080/realms/enterprise-ai")
    response = httpx.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": os.getenv("E2E_HUMAN_CLIENT_ID", "enterprise-ai-cli"),
            "username": os.getenv("E2E_HUMAN_USERNAME", "developer"),
            "password": os.getenv("E2E_HUMAN_PASSWORD", "development-only-password"),
            "scope": "openid profile email",
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


@pytest.fixture(scope="session")
def gateway_url() -> str:
    return os.getenv("E2E_GATEWAY_URL", "http://127.0.0.1:4000/v1")


@pytest.fixture(scope="session")
def gateway_root_url(gateway_url: str) -> str:
    parsed = urlsplit(gateway_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        raise ValueError("E2E_GATEWAY_URL must end in /v1")
    root_path = path[:-3].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, root_path, "", "")).rstrip("/")


@pytest.fixture(scope="session")
def service_token() -> str:
    deadline = time.monotonic() + 60
    while True:
        try:
            return _local_token()
        except (httpx.HTTPError, KeyError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


@pytest.fixture(scope="session")
def service_claims(service_token: str) -> dict[str, object]:
    return dict(jwt.decode(service_token, options={"verify_signature": False, "verify_aud": False}))


@pytest.fixture(scope="session")
def human_token() -> str:
    return _human_token()


@pytest.fixture(scope="session")
def human_claims(human_token: str) -> dict[str, object]:
    return dict(jwt.decode(human_token, options={"verify_signature": False, "verify_aud": False}))


@pytest.fixture(scope="session")
def admin_client(gateway_root_url: str) -> Iterator[httpx.Client]:
    hostname = urlsplit(gateway_root_url).hostname
    default_key = "replace-with-a-random-admin-key" if hostname in {"localhost", "127.0.0.1", "::1"} else ""
    master_key = os.getenv("E2E_LITELLM_MASTER_KEY", default_key)
    if not master_key:
        pytest.skip("set E2E_LITELLM_MASTER_KEY to test persisted accounting")
    with httpx.Client(
        base_url=gateway_root_url,
        headers={"Authorization": f"Bearer {master_key}"},
        timeout=20,
    ) as client:
        yield client


@pytest.fixture
def gateway_client(gateway_url: str, service_token: str) -> Iterator[OpenAI]:
    with OpenAI(base_url=gateway_url, api_key=service_token, timeout=20, max_retries=0) as client:
        yield client
