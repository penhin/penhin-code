from __future__ import annotations

import os
from typing import Any

from anthropic import Anthropic, APIConnectionError, InternalServerError, RateLimitError

from providers.types import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class AnthropicProvider:
    retry_errors = (APIConnectionError, RateLimitError, InternalServerError)

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url,
        )

    @classmethod
    def from_env(cls) -> "AnthropicProvider":
        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            base_url=os.getenv("ANTHROPIC_BASE_URL"),
        )

    def create_message(self, request: LLMRequest) -> LLMResponse:
        return normalize_response(
            self.client.messages.create(**request_kwargs(request))
        )

    def stream_message(
        self,
        request: LLMRequest,
        stream_callback: StreamCallback,
    ) -> LLMResponse:
        with self.client.messages.stream(**request_kwargs(request)) as stream:
            for event in stream:
                text = stream_event_text_delta(event)
                if text:
                    stream_callback(text)
            return normalize_response(stream.get_final_message())


def request_kwargs(request: LLMRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": request.model,
        "system": request.system,
        "messages": request.messages,
        "max_tokens": request.max_tokens,
    }
    if request.tools is not None:
        kwargs["tools"] = request.tools
    return kwargs


def normalize_response(response) -> LLMResponse:
    usage = getattr(response, "usage", None)
    return LLMResponse(
        content=[normalize_content_block(block) for block in getattr(response, "content", [])],
        stop_reason=str(getattr(response, "stop_reason", "") or ""),
        usage=LLMUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_input_tokens=_optional_int(usage, "cache_read_input_tokens"),
            cache_creation_input_tokens=_optional_int(usage, "cache_creation_input_tokens"),
        ),
    )


def _optional_int(value: Any, name: str) -> int | None:
    item = getattr(value, name, None)
    return int(item) if item is not None else None


def normalize_content_block(block) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)

    if hasattr(block, "model_dump"):
        data = block.model_dump(mode="json", exclude_none=True)
        if isinstance(data, dict):
            return data

    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {
            "type": "text",
            "text": str(getattr(block, "text", "")),
        }
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}),
        }
    return {
        "type": str(block_type or block.__class__.__name__),
        "value": str(block),
    }


def stream_event_text_delta(event) -> str:
    if getattr(event, "type", None) != "content_block_delta":
        return ""

    delta = getattr(event, "delta", None)
    if getattr(delta, "type", None) != "text_delta":
        return ""

    text = getattr(delta, "text", "")
    return text if isinstance(text, str) else ""
