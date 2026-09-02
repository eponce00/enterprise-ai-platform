"""Deterministic OpenAI-compatible backend for free CI and local tests."""

from __future__ import annotations

import json
import os
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


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
        if "force-total-failure" in prompt or ("force-provider-failure" in prompt and model == "gpt-4o-mini"):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"message": "injected provider failure"}})
            return
        if body.get("stream"):
            self._stream(model)
            return
        self._json(HTTPStatus.OK, completion(model, body))

    def _stream(self, model: str) -> None:
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        chunks = [
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "mock "}, "finish_reason": None}],
            },
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": "stream"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        ]
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
