from types import SimpleNamespace

from penhin.providers.deepseek import DeepSeekProvider, chat_messages, normalize_response, request_kwargs
from penhin.providers.protocols import LLMRequest


def request(messages=None, tools=None):
    return LLMRequest(
        model="deepseek-v4-pro", system="be helpful",
        messages=messages or [{"role": "user", "content": "hello"}],
        max_tokens=123, tools=tools,
    )


def test_deepseek_request_uses_chat_completions_shape() -> None:
    kwargs = request_kwargs(request(tools=[{"name": "read", "description": "Read", "input_schema": {"type": "object"}}]))

    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["messages"][:2] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]
    assert kwargs["max_tokens"] == 123
    assert "reasoning_effort" not in kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["tools"] == [{
        "type": "function",
        "function": {"name": "read", "description": "Read", "parameters": {"type": "object"}, "strict": False},
    }]


def test_deepseek_request_maps_selected_thinking_level() -> None:
    high = request_kwargs(LLMRequest(
        model="deepseek-v4-pro", system="help", messages=[], max_tokens=10, thinking_level="max",
    ))
    off = request_kwargs(LLMRequest(
        model="deepseek-v4-pro", system="help", messages=[], max_tokens=10, thinking_level="off",
    ))

    assert "reasoning_effort" not in high
    assert high["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in off
    assert off["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_messages_preserve_tool_calls_and_results() -> None:
    messages = chat_messages(request(messages=[
        {"role": "assistant", "content": [{"type": "reasoning", "text": "need a file"}, {"type": "text", "text": "checking"}, {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "contents"}]},
    ]))

    assert messages[1]["tool_calls"][0]["function"]["name"] == "read"
    assert messages[1]["reasoning_content"] == "need a file"
    assert messages[2] == {"role": "tool", "tool_call_id": "call-1", "content": "contents"}


def test_deepseek_tool_replay_keeps_required_empty_reasoning_content() -> None:
    messages = chat_messages(request(messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}},
        ]},
    ]))

    assert messages[1]["content"] == ""
    assert messages[1]["reasoning_content"] == ""


def test_deepseek_normalizes_tools_and_usage() -> None:
    call = SimpleNamespace(id="call-1", function=SimpleNamespace(name="read", arguments='{"path":"a.py"}'))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", reasoning_content="checking", tool_calls=[call]), finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, prompt_cache_hit_tokens=3),
    )

    normalized = normalize_response(response)

    assert normalized.content == [
        {"type": "reasoning", "text": "checking"},
        {"type": "text", "text": "done"},
        {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}},
    ]
    assert normalized.stop_reason == "tool_use"
    assert normalized.usage.input_tokens == 10
    assert normalized.usage.output_tokens == 4
    assert normalized.usage.cache_read_input_tokens == 3


def test_deepseek_stream_accumulates_text_tool_deltas_and_usage() -> None:
    chunks = [
        SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content=None, reasoning_content="think", tool_calls=[]))]),
        SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content="hi", reasoning_content=None, tool_calls=[]))]),
        SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(name="read", arguments='{"path":'))]))]),
        SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="tool_calls", delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='"a.py"}'))]))]),
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, prompt_cache_hit_tokens=1), choices=[]),
    ]
    provider = object.__new__(DeepSeekProvider)
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: iter(chunks))))
    streamed = []

    response = provider.stream_message(request(), streamed.append)

    assert streamed == ["hi"]
    assert response.content[0] == {"type": "reasoning", "text": "think"}
    assert response.content[2] == {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}}
    assert response.usage.input_tokens == 2
