from __future__ import annotations

import json

import httpx

from providers.openai_codex import OpenAICodexProvider, codex_request_body
from providers.types import LLMRequest


def request() -> LLMRequest:
    return LLMRequest(
        model="gpt-5-test", system="system", messages=[{"role": "user", "content": "hello"}],
        max_tokens=100, tools=[{"name": "read", "description": "Read", "input_schema": {"type": "object"}}],
    )


def test_codex_request_uses_responses_shape() -> None:
    body = codex_request_body(request())
    assert body["model"] == "gpt-5-test"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["tools"][0]["type"] == "function"


def test_codex_sse_normalizes_text_tool_calls_and_usage() -> None:
    events = [
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.function_call_arguments.done", "item_id": "item", "call_id": "call", "name": "read", "arguments": '{"path":"README.md"}'},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 2}}}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/backend-api/codex/responses"
        assert request.headers["authorization"] == "Bearer access-secret"
        assert request.headers["chatgpt-account-id"] == "account"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    chunks: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OpenAICodexProvider("access-secret", "account", "https://example.test/backend-api", client).stream_message(request(), chunks.append)
    assert chunks == ["hel", "lo"]
    assert result.content == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "call", "name": "read", "input": {"path": "README.md"}},
    ]
    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_input_tokens == 2


def test_codex_http_error_does_not_include_body() -> None:
    from providers.openai_codex import CodexHTTPError
    import pytest

    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="access-secret leaked"))
    with httpx.Client(transport=transport) as client, pytest.raises(CodexHTTPError) as caught:
        OpenAICodexProvider("access-secret", "account", "https://example.test", client).create_message(request())
    assert "access-secret leaked" not in str(caught.value)
