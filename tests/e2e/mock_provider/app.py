"""Deterministic OpenAI-compatible backend for free CI and local tests."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any

_READ_TOOL_SENTINEL = re.compile(r"(?:^|\s)force-opencode-read-tool path=(/[A-Za-z0-9._/-]{1,1024})(?=\s|$)")


class Handler(BaseHTTPRequestHandler):
    server_version = "EnterpriseAIMock/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path in {"/v1/models", "/models"}:
            self._json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o-mini", "object": "model", "owned_by": "mock-provider"},
                        {"id": "gpt-4o", "object": "model", "owned_by": "mock-provider"},
                    ],
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
            return
        expected = os.getenv("MOCK_PROVIDER_API_KEY", "mock-provider-key")
        if self.headers.get("authorization") != f"Bearer {expected}":
            self._json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "invalid provider key"}})
            return
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = str(body.get("model") or "gpt-4o-mini")
        prompt = " ".join(
            str(message.get("content") or "") for message in body.get("messages", []) if isinstance(message, dict)
        )
        if "assert-openrouter-privacy-routing" in prompt:
            provider = body.get("provider")
            if not isinstance(provider, dict) or not (
                provider.get("zdr") is True
                and provider.get("data_collection") == "deny"
                and provider.get("require_parameters") is True
            ):
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": {"message": "OpenRouter privacy policy was not forwarded"}},
                )
                return
        if "assert-direct-privacy-routing" in prompt and "provider" in body:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"error": {"message": "OpenRouter-only privacy fields leaked to a direct backend"}},
            )
            return
        if "force-total-failure" in prompt or ("force-provider-failure" in prompt and model == "gpt-4o-mini"):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"message": "injected provider failure"}})
            return
        if body.get("stream"):
            self._stream(model, body)
            return
        self._json(HTTPStatus.OK, completion(model, body))

    def _stream(self, model: str, body: dict[str, Any]) -> None:
        chunks = streaming_chunks(model, body)
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        encoded = payload.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def streaming_chunks(model: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    messages = body.get("messages", [])
    prompt = " ".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    read_path = _read_tool_path(prompt)
    read_result = _tool_result(messages, "call_opencode_read")
    if read_path is not None and read_result is not None:
        return _streamed_text_chunks(
            request_id,
            created,
            model,
            "mock observed OpenCode read result: ",
            read_result,
        )
    if read_path is not None and _advertises_function(body, "read"):
        return _streamed_tool_call_chunks(
            request_id,
            created,
            model,
            call_id="call_opencode_read",
            function_name="read",
            arguments=json.dumps({"filePath": read_path}, separators=(",", ":")),
        )

    tool_result = _tool_result(messages)
    if "force-streaming-tool-call" in prompt and tool_result is not None:
        return _streamed_text_chunks(request_id, created, model, "mock observed tool result: ", tool_result)
    if body.get("tools") and "force-streaming-tool-call" in prompt:
        return _streamed_tool_call_chunks(
            request_id,
            created,
            model,
            call_id="call_stream_mock",
            function_name="lookup",
            arguments='{"value":"mock"}',
        )
    return _streamed_text_chunks(request_id, created, model, "mock ", "stream")


def _read_tool_path(prompt: str) -> str | None:
    match = _READ_TOOL_SENTINEL.search(prompt)
    if match is None:
        return None
    path = match.group(1)
    parsed = PurePosixPath(path)
    if path.startswith("//") or parsed.as_posix() != path or any(part in {".", ".."} for part in parsed.parts):
        return None
    return path


def _advertises_function(body: dict[str, Any], name: str) -> bool:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == name
        for tool in tools
    )


def _tool_result(messages: object, call_id: str | None = None) -> str | None:
    if not isinstance(messages, list):
        return None
    return next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and (call_id is None or message.get("tool_call_id") == call_id)
        ),
        None,
    )


def _streamed_text_chunks(
    request_id: str,
    created: int,
    model: str,
    prefix: str,
    content: str,
) -> list[dict[str, Any]]:
    return [
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": prefix}, "finish_reason": None}],
        },
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        },
    ]


def _streamed_tool_call_chunks(
    request_id: str,
    created: int,
    model: str,
    *,
    call_id: str,
    function_name: str,
    arguments: str,
) -> list[dict[str, Any]]:
    return [
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": function_name, "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        },
    ]


def completion(model: str, body: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "mock completion"}
    finish_reason = "stop"
    if body.get("tools"):
        function = body["tools"][0]["function"]
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock",
                    "type": "function",
                    "function": {"name": function["name"], "arguments": '{"value":"mock"}'},
                }
            ],
        }
        finish_reason = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()  # noqa: S104
