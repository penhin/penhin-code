from __future__ import annotations

import json
from typing import Any

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

from .protocols import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class DeepSeekProvider:
    """DeepSeek Chat Completions adapter with native tool-call normalization."""

    retry_errors = (APIConnectionError, InternalServerError, RateLimitError)

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com") -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def create_message(self, request: LLMRequest) -> LLMResponse:
        response = self.client.chat.completions.create(**request_kwargs(request))
        return normalize_response(response)

    def stream_message(self, request: LLMRequest, stream_callback: StreamCallback) -> LLMResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        kwargs = request_kwargs(request)
        kwargs.update(stream=True, stream_options={"include_usage": True})
        for chunk in self.client.chat.completions.create(**kwargs):
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", ())
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = choice.delta
            content = getattr(delta, "content", None)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
            if content:
                text_parts.append(content)
                stream_callback(content)
            for tool_call in getattr(delta, "tool_calls", None) or ():
                current = calls.setdefault(tool_call.index, {"id": "", "name": "", "arguments": ""})
                current["id"] += getattr(tool_call, "id", None) or ""
                function = getattr(tool_call, "function", None)
                if function is not None:
                    current["name"] += getattr(function, "name", None) or ""
                    current["arguments"] += getattr(function, "arguments", None) or ""
        return response_from_parts("".join(text_parts), list(calls.values()), usage, finish_reason, "".join(reasoning_parts))


def request_kwargs(request: LLMRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": request.model,
        "messages": chat_messages(request),
        "max_tokens": request.max_tokens,
    }
    thinking_level = request.thinking_level or "high"
    if thinking_level == "off":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    else:
        if thinking_level not in {"high", "max"}:
            raise ValueError(f"unsupported DeepSeek thinking level: {thinking_level}")
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    if request.tools:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                    "strict": False,
                },
            }
            for tool in request.tools
        ]
    return kwargs


def chat_messages(request: LLMRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    for message in request.messages:
        role, content = message.get("role"), message.get("content")
        if role == "assistant" and isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
            reasoning = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "reasoning")
            tool_calls = [
                {
                    "id": block["id"], "type": "function",
                    "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}))},
                }
                for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            item: dict[str, Any] = {"role": "assistant", "content": text}
            if reasoning or tool_calls:
                item["reasoning_content"] = reasoning
            if tool_calls:
                item["tool_calls"] = tool_calls
            messages.append(item)
        elif role == "user" and isinstance(content, list):
            messages.extend(
                {"role": "tool", "tool_call_id": block["tool_use_id"], "content": str(block.get("content", ""))}
                for block in content if isinstance(block, dict) and block.get("type") == "tool_result"
            )
        elif role in {"user", "assistant"}:
            messages.append({"role": role, "content": str(content)})
    return messages


def normalize_response(response: Any) -> LLMResponse:
    choice = response.choices[0]
    message = choice.message
    calls = [
        {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
        for call in getattr(message, "tool_calls", None) or ()
    ]
    return response_from_parts(
        str(message.content or ""), calls, getattr(response, "usage", None), choice.finish_reason,
        str(getattr(message, "reasoning_content", "") or ""),
    )


def response_from_parts(text: str, calls: list[dict[str, str]], usage: Any, finish_reason: str | None, reasoning: str = "") -> LLMResponse:
    content: list[dict[str, Any]] = ([{"type": "reasoning", "text": reasoning}] if reasoning else [])
    if text:
        content.append({"type": "text", "text": text})
    for call in calls:
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
    cached = getattr(usage, "prompt_cache_hit_tokens", None)
    return LLMResponse(
        content=content,
        stop_reason="tool_use" if calls else (finish_reason or "end_turn"),
        usage=LLMUsage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cache_read_input_tokens=int(cached) if cached is not None else None,
        ),
    )


__all__ = ["DeepSeekProvider"]
