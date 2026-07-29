from __future__ import annotations

import json
import os
from typing import Any

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

from providers.types import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class OpenAIProvider:
    retry_errors = (APIConnectionError, InternalServerError, RateLimitError)

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    @classmethod
    def from_env(cls) -> "OpenAIProvider":
        return cls(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))

    def create_message(self, request: LLMRequest) -> LLMResponse:
        response = self.client.chat.completions.create(**request_kwargs(request))
        return normalize_response(response)

    def stream_message(self, request: LLMRequest, stream_callback: StreamCallback) -> LLMResponse:
        stream = self.client.chat.completions.create(**request_kwargs(request), stream=True)
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage = None
        for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
            if choice is None:
                continue
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                stream_callback(text)
            for call in getattr(delta, "tool_calls", None) or []:
                item = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                item["id"] = call.id or item["id"]
                function = getattr(call, "function", None)
                if function is not None:
                    item["name"] = function.name or item["name"]
                    item["arguments"] += function.arguments or ""
        return response_from_parts("".join(content_parts), list(calls.values()), finish_reason, usage)


def request_kwargs(request: LLMRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": request.model,
        "messages": openai_messages(request.system, request.messages),
        "max_tokens": request.max_tokens,
    }
    if request.tools is not None:
        kwargs["tools"] = [
            {"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}
            for tool in request.tools
        ]
    return kwargs


def openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "assistant" and isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
            calls = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call.get("input", {}))}} for call in calls]
            projected.append(assistant)
        elif role == "user" and isinstance(content, list):
            for result in content:
                if isinstance(result, dict) and result.get("type") == "tool_result":
                    projected.append({"role": "tool", "tool_call_id": result["tool_use_id"], "content": str(result.get("content", ""))})
        elif role in {"user", "assistant"}:
            projected.append({"role": role, "content": content if isinstance(content, str) else str(content)})
    return projected


def normalize_response(response: Any) -> LLMResponse:
    choice = response.choices[0]
    message = choice.message
    calls = [{"id": call.id, "name": call.function.name, "arguments": call.function.arguments} for call in message.tool_calls or []]
    return response_from_parts(message.content or "", calls, choice.finish_reason or "", getattr(response, "usage", None))


def response_from_parts(text: str, calls: list[dict[str, str]], finish_reason: str, usage: Any) -> LLMResponse:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in calls:
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
    return LLMResponse(
        content=content,
        stop_reason="tool_use" if calls or finish_reason == "tool_calls" else str(finish_reason),
        usage=LLMUsage(input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0), output_tokens=int(getattr(usage, "completion_tokens", 0) or 0)),
    )
