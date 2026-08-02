from __future__ import annotations

import json
from typing import Any

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

from penhin.providers.protocols import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class OpenAIProvider:
    retry_errors = (APIConnectionError, InternalServerError, RateLimitError)

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def create_message(self, request: LLMRequest) -> LLMResponse:
        return normalize_response(self.client.responses.create(**request_kwargs(request)))

    def stream_message(self, request: LLMRequest, stream_callback: StreamCallback) -> LLMResponse:
        completed = None
        calls: dict[str, dict[str, str]] = {}
        for event in self.client.responses.create(**request_kwargs(request), stream=True):
            if event.type == "response.output_text.delta":
                stream_callback(event.delta)
            elif event.type == "response.function_call_arguments.done":
                calls[event.item_id] = {"id": event.call_id, "name": event.name, "arguments": event.arguments}
            elif event.type == "response.completed":
                completed = event.response
        if completed is None:
            return response_from_parts("", list(calls.values()), None)
        response = normalize_response(completed)
        return response if response.content else response_from_parts("", list(calls.values()), completed.usage)


def request_kwargs(request: LLMRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "input": responses_input(request.messages),
        "max_output_tokens": request.max_tokens,
        "store": False,
    }
    if request.tools:
        kwargs["tools"] = [
            {"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"], "strict": False}
            for tool in request.tools
        ]
    return kwargs


def responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "assistant" and isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            items.extend({"type": "function_call", "call_id": block["id"], "name": block["name"], "arguments": json.dumps(block.get("input", {}))} for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
        elif role == "user" and isinstance(content, list):
            items.extend({"type": "function_call_output", "call_id": block["tool_use_id"], "output": str(block.get("content", ""))} for block in content if isinstance(block, dict) and block.get("type") == "tool_result")
        elif role in {"user", "assistant"}:
            items.append({"role": role, "content": [{"type": "input_text", "text": str(content)}]})
    return items


def normalize_response(response: Any) -> LLMResponse:
    text = str(getattr(response, "output_text", "") or "")
    calls = [{"id": item.call_id, "name": item.name, "arguments": item.arguments} for item in getattr(response, "output", []) if getattr(item, "type", "") == "function_call"]
    return response_from_parts(text, calls, getattr(response, "usage", None))


def response_from_parts(text: str, calls: list[dict[str, str]], usage: Any) -> LLMResponse:
    content: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else [])
    for call in calls:
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    cached = getattr(input_details, "cached_tokens", None)
    reasoning = getattr(output_details, "reasoning_tokens", None)
    return LLMResponse(content=content, stop_reason="tool_use" if calls else "end_turn", usage=LLMUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(cached) if cached is not None else None,
        reasoning_tokens=int(reasoning) if reasoning is not None else None,
    ))
