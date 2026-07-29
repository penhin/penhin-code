from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from google import genai
from google.genai import types

from providers.types import LLMRequest, LLMResponse, LLMUsage, StreamCallback


class GeminiProvider:
    # The Google SDK already retries transient failures with exponential backoff.
    retry_errors: tuple[type[BaseException], ...] = ()

    def __init__(self, api_key: str | None = None):
        self.client = genai.Client(api_key=api_key)

    @classmethod
    def from_env(cls) -> "GeminiProvider":
        return cls(api_key=os.getenv("GEMINI_API_KEY"))

    def create_message(self, request: LLMRequest) -> LLMResponse:
        return normalize_response(self.client.models.generate_content(**request_kwargs(request)))

    def stream_message(self, request: LLMRequest, stream_callback: StreamCallback) -> LLMResponse:
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage = None
        for chunk in self.client.models.generate_content_stream(**request_kwargs(request)):
            usage = getattr(chunk, "usage_metadata", None) or usage
            for part in response_parts(chunk):
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)
                    stream_callback(text)
                call = getattr(part, "function_call", None)
                if call:
                    calls.append({"name": call.name, "args": dict(call.args or {})})
        return response_from_parts("".join(text_parts), calls, usage)


def request_kwargs(request: LLMRequest) -> dict[str, Any]:
    declarations = [types.FunctionDeclaration(name=tool["name"], description=tool["description"], parameters_json_schema=tool["input_schema"]) for tool in request.tools or []]
    config = types.GenerateContentConfig(
        system_instruction=request.system,
        max_output_tokens=request.max_tokens,
        tools=[types.Tool(function_declarations=declarations)] if declarations else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    return {"model": request.model, "contents": gemini_contents(request.messages), "config": config}


def gemini_contents(messages: list[dict[str, Any]]) -> list[types.Content]:
    contents: list[types.Content] = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "assistant" and isinstance(content, list):
            parts = [types.Part(text=block["text"]) for block in content if isinstance(block, dict) and block.get("type") == "text"]
            parts += [types.Part(function_call=types.FunctionCall(name=block["name"], args=block.get("input", {}))) for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            contents.append(types.Content(role="model", parts=parts))
        elif role == "user" and isinstance(content, list):
            parts = [types.Part.from_function_response(name=block.get("tool_name", "tool"), response={"result": block.get("content", "")}) for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
            if parts:
                contents.append(types.Content(role="tool", parts=parts))
        elif role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=str(content))]))
    return contents


def response_parts(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    return list(getattr(getattr(candidates[0], "content", None), "parts", None) or []) if candidates else []


def normalize_response(response: Any) -> LLMResponse:
    text = "".join(str(getattr(part, "text", "") or "") for part in response_parts(response))
    calls = [{"name": call.name, "args": dict(call.args or {})} for call in getattr(response, "function_calls", None) or []]
    if not calls:
        calls = [{"name": part.function_call.name, "args": dict(part.function_call.args or {})} for part in response_parts(response) if getattr(part, "function_call", None)]
    return response_from_parts(text, calls, getattr(response, "usage_metadata", None))


def response_from_parts(text: str, calls: list[dict[str, Any]], usage: Any) -> LLMResponse:
    content: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else [])
    content.extend({"type": "tool_use", "id": f"gemini-{uuid4().hex}", "name": call["name"], "input": call["args"]} for call in calls)
    cached = getattr(usage, "cached_content_token_count", None)
    reasoning = getattr(usage, "thoughts_token_count", None)
    return LLMResponse(content=content, stop_reason="tool_use" if calls else "end_turn", usage=LLMUsage(
        input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        cache_read_input_tokens=int(cached) if cached is not None else None,
        reasoning_tokens=int(reasoning) if reasoning is not None else None,
    ))
