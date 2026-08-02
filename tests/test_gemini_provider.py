from types import SimpleNamespace

from penhin.providers.gemini import GeminiProvider, gemini_contents, normalize_response, request_kwargs
from penhin.providers.protocols import LLMRequest


def request(messages=None, tools=None):
    return LLMRequest(
        model="gemini-2.5-flash",
        system="be helpful",
        messages=messages or [{"role": "user", "content": "hello"}],
        max_tokens=123,
        tools=tools,
    )


def test_request_uses_gemini_system_tools_and_contents() -> None:
    kwargs = request_kwargs(request(tools=[{"name": "read", "description": "Read a file", "input_schema": {"type": "object"}}]))

    assert kwargs["model"] == "gemini-2.5-flash"
    assert kwargs["config"].system_instruction == "be helpful"
    assert kwargs["config"].max_output_tokens == 123
    assert kwargs["config"].tools[0].function_declarations[0].name == "read"
    assert kwargs["contents"][0].role == "user"
    assert kwargs["contents"][0].parts[0].text == "hello"


def test_gemini_contents_preserves_tool_results() -> None:
    contents = gemini_contents([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "tool_name": "read", "content": "contents"}]},
    ])

    assert contents[0].role == "model"
    assert contents[0].parts[0].function_call.name == "read"
    assert contents[1].role == "tool"
    assert contents[1].parts[0].function_response.name == "read"


def test_normalize_response_handles_text_tools_and_usage() -> None:
    part = SimpleNamespace(text=None, function_call=SimpleNamespace(name="read", args={"path": "a.py"}))
    response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="done", function_call=None), part]))],
        function_calls=[],
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=4),
    )

    normalized = normalize_response(response)

    assert normalized.content[0] == {"type": "text", "text": "done"}
    assert normalized.content[1]["name"] == "read"
    assert normalized.content[1]["input"] == {"path": "a.py"}
    assert normalized.stop_reason == "tool_use"
    assert normalized.usage.input_tokens == 10
    assert normalized.usage.output_tokens == 4


def test_stream_message_emits_text_and_tool_calls() -> None:
    chunk = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="hello", function_call=None), SimpleNamespace(text=None, function_call=SimpleNamespace(name="read", args={"path": "a.py"}))]))],
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2),
    )
    provider = object.__new__(GeminiProvider)
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=lambda **kwargs: iter([chunk])))
    chunks = []

    response = provider.stream_message(request(), chunks.append)

    assert chunks == ["hello"]
    assert response.content[0] == {"type": "text", "text": "hello"}
    assert response.content[1]["name"] == "read"

