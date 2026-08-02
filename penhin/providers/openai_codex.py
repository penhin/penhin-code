from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import httpx

from penhin.providers.openai import responses_input
from penhin.providers.protocols import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class CodexHTTPError(RuntimeError):
    pass


class CodexRetryableError(CodexHTTPError):
    pass


class OpenAICodexProvider:
    retry_errors = (httpx.ConnectError, httpx.TimeoutException, CodexRetryableError)

    def __init__(self, access_token: str, account_id: str, base_url: str | None = None, client: httpx.Client | None = None):
        self.access_token = access_token
        self.account_id = account_id
        self.base_url = (base_url or os.getenv("PENHIN_OPENAI_CODEX_BASE_URL", "https://chatgpt.com/backend-api")).rstrip("/")
        self.client = client or httpx.Client(timeout=120)

    def create_message(self, request: LLMRequest) -> LLMResponse:
        return self.stream_message(request, lambda _text: None)

    def stream_message(self, request: LLMRequest, stream_callback: StreamCallback) -> LLMResponse:
        request_id = str(uuid4())
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "chatgpt-account-id": self.account_id,
            "originator": "pi",
            "User-Agent": "penhin-code",
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "session-id": request_id,
            "x-client-request-id": request_id,
        }
        body = codex_request_body(request)
        text_parts: list[str] = []
        calls: dict[str, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        with self.client.stream("POST", f"{self.base_url}/codex/responses", headers=headers, json=body) as response:
            if not response.is_success:
                error_type = CodexRetryableError if response.status_code == 429 or response.status_code >= 500 else CodexHTTPError
                raise error_type(f"OpenAI Codex request failed with HTTP {response.status_code}")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    text_parts.append(event["delta"])
                    stream_callback(event["delta"])
                elif kind == "response.function_call_arguments.done":
                    item_id = str(event.get("item_id") or event.get("call_id") or uuid4())
                    calls[item_id] = {"id": str(event.get("call_id") or item_id), "name": str(event.get("name") or ""), "arguments": str(event.get("arguments") or "{}")}
                elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                    item = event["item"]
                    if item.get("type") == "function_call":
                        item_id = str(item.get("id") or item.get("call_id") or uuid4())
                        calls[item_id] = {"id": str(item.get("call_id") or item_id), "name": str(item.get("name") or ""), "arguments": str(item.get("arguments") or "{}")}
                elif kind == "response.completed" and isinstance(event.get("response"), dict):
                    usage = event["response"].get("usage") or {}
        content: list[dict[str, Any]] = []
        if text_parts:
            content.append({"type": "text", "text": "".join(text_parts)})
        for call in calls.values():
            try:
                arguments = json.loads(call["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        return LLMResponse(
            content=content,
            stop_reason="tool_use" if calls else "end_turn",
            usage=LLMUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_input_tokens=_optional_int(input_details.get("cached_tokens")),
                reasoning_tokens=_optional_int(output_details.get("reasoning_tokens")),
            ),
        )


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def codex_request_body(request: LLMRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "input": responses_input(request.messages),
        "store": False,
        "stream": True,
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if request.tools:
        body["tools"] = [
            {"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"], "strict": False}
            for tool in request.tools
        ]
    return body
