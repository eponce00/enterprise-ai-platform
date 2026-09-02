from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from openai import APIError, APIStatusError, OpenAI
from openai.types.chat import ChatCompletionFunctionToolParam

pytestmark = pytest.mark.integration


def test_normal_completion_and_accounting(gateway_client: OpenAI) -> None:
    raw_response = gateway_client.chat.completions.with_raw_response.create(
        model="general-fast",
        messages=[{"role": "user", "content": "hello"}],
    )
    response = raw_response.parse()
    assert response.choices[0].message.content == "mock completion"
    assert response.usage is not None
    assert response.usage.total_tokens == 6
    assert float(raw_response.headers["x-litellm-response-cost"]) > 0


def test_streaming(gateway_client: OpenAI) -> None:
    stream = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "stream"}],
        stream=True,
    )
    assert "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices) == "mock stream"


def test_streaming_tool_call_round_trip(gateway_client: OpenAI) -> None:
    tools: list[ChatCompletionFunctionToolParam] = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
            },
        }
    ]
    stream = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "force-streaming-tool-call"}],
        tools=tools,
        stream=True,
    )
    call_id = ""
    function_name = ""
    function_arguments = ""
    finish_reasons = []
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reasons.append(choice.finish_reason)
        for tool_call in choice.delta.tool_calls or []:
            call_id = tool_call.id or call_id
            if tool_call.function:
                function_name += tool_call.function.name or ""
                function_arguments += tool_call.function.arguments or ""

    assert call_id == "call_stream_mock"
    assert function_name == "lookup"
    assert json.loads(function_arguments) == {"value": "mock"}
    assert "tool_calls" in finish_reasons

    follow_up = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[
            {"role": "user", "content": "force-streaming-tool-call"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": function_name, "arguments": function_arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "lookup-result=mock"},
        ],
        tools=tools,
        stream=True,
    )
    content = "".join(chunk.choices[0].delta.content or "" for chunk in follow_up if chunk.choices)
    assert content == "mock observed tool result: lookup-result=mock"


def test_opencode_read_tool_streaming_round_trip(gateway_client: OpenAI) -> None:
    read_path = "/workspace/fixtures/opencode-read.txt"
    tools: list[ChatCompletionFunctionToolParam] = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"filePath": {"type": "string"}},
                    "required": ["filePath"],
                },
            },
        }
    ]
    stream = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": f"force-opencode-read-tool path={read_path}"}],
        tools=tools,
        stream=True,
    )
    call_id = ""
    function_name = ""
    function_arguments = ""
    finish_reasons = []
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reasons.append(choice.finish_reason)
        for tool_call in choice.delta.tool_calls or []:
            call_id = tool_call.id or call_id
            if tool_call.function:
                function_name += tool_call.function.name or ""
                function_arguments += tool_call.function.arguments or ""

    assert call_id == "call_opencode_read"
    assert function_name == "read"
    assert json.loads(function_arguments) == {"filePath": read_path}
    assert "tool_calls" in finish_reasons

    follow_up = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[
            {"role": "user", "content": f"force-opencode-read-tool path={read_path}"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": function_name, "arguments": function_arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "client-read-marker"},
        ],
        tools=tools,
        stream=True,
    )
    content = "".join(chunk.choices[0].delta.content or "" for chunk in follow_up if chunk.choices)
    assert content == "mock observed OpenCode read result: client-read-marker"


def test_opencode_read_tool_requires_safe_path_and_advertised_function(gateway_client: OpenAI) -> None:
    read_tools: list[ChatCompletionFunctionToolParam] = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    invalid_path = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "force-opencode-read-tool path=/workspace/../secret"}],
        tools=read_tools,
        stream=True,
    )
    assert "".join(chunk.choices[0].delta.content or "" for chunk in invalid_path if chunk.choices) == "mock stream"

    lookup_tools: list[ChatCompletionFunctionToolParam] = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    unadvertised = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "force-opencode-read-tool path=/workspace/safe.txt"}],
        tools=lookup_tools,
        stream=True,
    )
    assert "".join(chunk.choices[0].delta.content or "" for chunk in unadvertised if chunk.choices) == "mock stream"


def test_tool_call(gateway_client: OpenAI) -> None:
    response = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "call the tool"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
                },
            }
        ],
    )
    calls = response.choices[0].message.tool_calls
    assert calls and calls[0].function.name == "lookup"


def test_provider_failure_uses_configured_fallback(gateway_client: OpenAI) -> None:
    response = gateway_client.chat.completions.create(
        model="general-fast",
        messages=[{"role": "user", "content": "force-provider-failure"}],
    )
    assert response.model == "gpt-4o"


def test_total_provider_failure_is_reported(gateway_client: OpenAI) -> None:
    with pytest.raises(APIError):
        gateway_client.chat.completions.create(
            model="general-fast",
            messages=[{"role": "user", "content": "force-total-failure"}],
        )


def test_models_are_visible(gateway_client: OpenAI) -> None:
    ids = {model.id for model in gateway_client.models.list().data}
    assert {"general-fast", "cheap-batch"}.issubset(ids)


def test_team_model_policy_denies_disallowed_alias(gateway_client: OpenAI) -> None:
    with pytest.raises(APIStatusError) as error:
        gateway_client.chat.completions.create(
            model="coding-frontier",
            messages=[{"role": "user", "content": "this service must not access coding models"}],
        )
    assert error.value.status_code == 403


def test_human_identity_can_call_gateway(gateway_url: str, human_token: str) -> None:
    with OpenAI(base_url=gateway_url, api_key=human_token, timeout=20, max_retries=0) as client:
        response = client.chat.completions.create(
            model="general-fast",
            messages=[{"role": "user", "content": "human identity check"}],
        )
    assert response.choices[0].message.content == "mock completion"


def test_human_attribution_is_persisted(
    gateway_url: str,
    human_token: str,
    human_claims: dict[str, object],
    admin_client: httpx.Client,
) -> None:
    with OpenAI(base_url=gateway_url, api_key=human_token, timeout=20, max_retries=0) as client:
        raw_response = client.chat.completions.with_raw_response.create(
            model="general-fast",
            messages=[{"role": "user", "content": "persist human attribution"}],
        )
        response = raw_response.parse()

    row = _wait_for_spend_log(admin_client, [response.id])
    issuer = str(human_claims["iss"]).rstrip("/")
    assert row["user"] == f"{issuer}|{human_claims['sub']}"
    assert row["team_id"] == "developers"
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert isinstance(metadata, dict)
    assert metadata["user_api_key_alias"] == "human/enterprise-ai-cli"


def test_persisted_attribution_and_team_budget_enforcement(
    gateway_client: OpenAI,
    gateway_url: str,
    service_claims: dict[str, object],
    admin_client: httpx.Client,
) -> None:
    sentinel = f"content-must-not-be-logged-{uuid4()}"
    raw_response = gateway_client.chat.completions.with_raw_response.create(
        model="general-fast",
        messages=[{"role": "user", "content": sentinel}],
    )
    parsed_response = raw_response.parse()
    call_id = raw_response.headers.get("x-litellm-call-id")
    assert call_id

    row = _wait_for_spend_log(admin_client, [parsed_response.id, call_id])
    issuer = str(service_claims["iss"]).rstrip("/")
    assert row["user"] == f"{issuer}|{service_claims['sub']}"
    assert row["team_id"] == "automation"
    assert row["model_group"] == "general-fast"
    assert row["custom_llm_provider"] == "openai"
    assert row["prompt_tokens"] == 4
    assert row["completion_tokens"] == 2
    assert row["total_tokens"] == 6
    assert float(row["spend"]) > 0
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert isinstance(metadata, dict)
    assert metadata["user_api_key_alias"] == "service/example-service"

    detail = _spend_log_detail(admin_client, str(row["request_id"]))
    assert isinstance(detail, dict)
    assert {"messages", "response", "proxy_server_request"}.issubset(detail)
    for field in ("messages", "response", "proxy_server_request"):
        value = detail[field]
        if isinstance(value, str):
            assert value.strip() in {"", "{}", "[]", "null"}
        else:
            assert value in (None, {}, [])
    assert sentinel not in json.dumps(detail, sort_keys=True)

    hostname = urlsplit(gateway_url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"} and os.getenv("ALLOW_E2E_POLICY_MUTATION") != "1":
        pytest.skip("set ALLOW_E2E_POLICY_MUTATION=1 to mutate a non-loopback test team's budget")

    team_info = _wait_for_team_spend(admin_client, "automation")
    original_budget = team_info.get("max_budget")
    assert isinstance(original_budget, int | float) and original_budget > 0
    current_spend = float(team_info["spend"])
    constrained_budget = max(current_spend / 2, 1e-12)
    changed = False
    try:
        update = admin_client.post(
            "/team/update",
            json={"team_id": "automation", "max_budget": constrained_budget},
        )
        update.raise_for_status()
        changed = True
        with pytest.raises(APIStatusError) as budget_error:
            gateway_client.chat.completions.create(
                model="general-fast",
                messages=[{"role": "user", "content": "budget should deny this request"}],
            )
        assert budget_error.value.status_code == 429
        assert "budget" in str(budget_error.value).lower()
    finally:
        if changed:
            restore = admin_client.post(
                "/team/update",
                json={"team_id": "automation", "max_budget": original_budget},
            )
            restore.raise_for_status()


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "services/examples/python/main.py"],
        ["node", "--experimental-strip-types", "services/examples/typescript/main.ts"],
    ],
)
def test_shipped_service_examples_run_unchanged(
    command: list[str],
    gateway_url: str,
) -> None:
    if command[0] == "node" and shutil.which("node") is None:
        pytest.skip("Node.js is required for the TypeScript service example")
    issuer = os.getenv("E2E_OIDC_ISSUER", "http://127.0.0.1:8080/realms/enterprise-ai")
    environment = {
        **os.environ,
        "OIDC_TOKEN_URL": f"{issuer}/protocol/openid-connect/token",
        "OIDC_CLIENT_ID": os.getenv("E2E_CLIENT_ID", "example-service"),
        "OIDC_CLIENT_SECRET": os.getenv("E2E_CLIENT_SECRET", "development-only-service-secret"),
        "GATEWAY_URL": gateway_url,
        "MODEL": "general-fast",
    }
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(  # noqa: S603 - fixed test commands, never user input
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "mock completion" in completed.stdout


def _wait_for_spend_log(admin_client: httpx.Client, request_ids: list[str]) -> dict[str, object]:
    deadline = time.monotonic() + 30
    now = datetime.now(timezone.utc)
    date_range = {
        "start_date": (now - timedelta(days=1)).date().isoformat(),
        "end_date": (now + timedelta(days=1)).date().isoformat(),
    }
    while True:
        for request_id in dict.fromkeys(request_ids):
            response = admin_client.get(
                "/spend/logs/v2",
                params={**date_range, "request_id": request_id, "page_size": 10},
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if rows:
                assert isinstance(rows[0], dict)
                return dict(rows[0])
        if time.monotonic() >= deadline:
            pytest.fail(f"spend log for {request_ids} was not persisted within 30 seconds")
        time.sleep(0.5)


def _spend_log_detail(admin_client: httpx.Client, request_id: str) -> object:
    response = admin_client.get(f"/spend/logs/ui/{request_id}")
    response.raise_for_status()
    return response.json()


def _wait_for_team_spend(admin_client: httpx.Client, team_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while True:
        response = admin_client.get("/team/info", params={"team_id": team_id})
        response.raise_for_status()
        payload = response.json()
        team_info = payload.get("team_info", {}) if isinstance(payload, dict) else {}
        if isinstance(team_info, dict) and float(team_info.get("spend") or 0) > 0:
            return dict(team_info)
        if time.monotonic() >= deadline:
            pytest.fail(f"team {team_id} spend was not updated within 30 seconds")
        time.sleep(0.5)
