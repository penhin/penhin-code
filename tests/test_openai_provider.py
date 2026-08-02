from types import SimpleNamespace

from penhin.providers.openai import OpenAIProvider, normalize_response, request_kwargs, responses_input
from penhin.providers.protocols import LLMRequest


def request(messages=None, tools=None):
    return LLMRequest(
        model="gpt-4.1",
        system="be helpful",
        messages=messages or [{"role": "user", "content": "hello"}],
        max_tokens=123,
        tools=tools,
    )


def test_request_uses_responses_api_shape() -> None:
    kwargs = request_kwargs(request(tools=[{"name": "read", "description": "Read a file", "input_schema": {"type": "object"}}]))

    assert kwargs["model"] == "gpt-4.1"
    assert kwargs["instructions"] == "be helpful"
    assert kwargs["max_output_tokens"] == 123
    assert kwargs["store"] is False
    assert kwargs["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    assert kwargs["tools"] == [{"type": "function", "name": "read", "description": "Read a file", "parameters": {"type": "object"}, "strict": False}]


def test_responses_input_preserves_tool_calls_and_results() -> None:
    assert responses_input([
        {"role": "assistant", "content": [{"type": "text", "text": "checking"}, {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "contents"}]},
    ]) == [
        {"role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
        {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path": "a.py"}'},
        {"type": "function_call_output", "call_id": "call-1", "output": "contents"},
    ]


def test_normalize_response_handles_text_tools_and_usage() -> None:
    response = SimpleNamespace(
        output_text="done",
        output=[SimpleNamespace(type="function_call", call_id="call-1", name="read", arguments='{"path":"a.py"}')],
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
    )

    normalized = normalize_response(response)

    assert normalized.content == [
        {"type": "text", "text": "done"},
        {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}},
    ]
    assert normalized.stop_reason == "tool_use"
    assert normalized.usage.input_tokens == 10
    assert normalized.usage.output_tokens == 4


def test_stream_message_normalizes_completed_response_and_emits_text() -> None:
    completed = SimpleNamespace(
        output_text="hello",
        output=[],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="hel"),
        SimpleNamespace(type="response.output_text.delta", delta="lo"),
        SimpleNamespace(type="response.completed", response=completed),
    ]
    provider = object.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter(events)))
    chunks = []

    response = provider.stream_message(request(), chunks.append)

    assert chunks == ["hel", "lo"]
    assert response.content == [{"type": "text", "text": "hello"}]
    assert response.usage.input_tokens == 3

