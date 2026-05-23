import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compact
import transcript
from compact import (
    auto_compact_messages,
    compact_source_text,
    micro_compact_text,
    recent_message_start,
    should_auto_compact,
)

from tests.helpers import ToolUseBlock


def test_micro_compact_text() -> None:
    long_output = "x" * 200
    preserved_output = "y" * 200
    recent_output = "z" * 200

    messages = [
        {
            "role": "assistant",
            "content": [
                ToolUseBlock("tool-1", "list"),
                ToolUseBlock("tool-2", "read"),
                ToolUseBlock("tool-3", "bash"),
                ToolUseBlock("tool-4", "search"),
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": long_output},
                {"type": "tool_result", "tool_use_id": "tool-2", "content": preserved_output},
                {"type": "tool_result", "tool_use_id": "tool-3", "content": recent_output},
                {"type": "tool_result", "tool_use_id": "tool-4", "content": recent_output},
            ],
        },
    ]

    micro_compact_text(messages, keep_recent=2)

    results = messages[1]["content"]
    assert results[0]["content"] == "[Previous: used list]"
    assert results[1]["content"] == preserved_output
    assert results[2]["content"] == recent_output
    assert results[3]["content"] == recent_output

    todo_messages = [
        {"role": "assistant", "content": [ToolUseBlock("tool-5", "todo_show")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-5", "content": long_output}],
        },
    ]
    micro_compact_text(todo_messages, keep_recent=0)
    assert todo_messages[1]["content"][0]["content"] == long_output


def test_auto_compact_helpers() -> None:
    assert should_auto_compact([{"role": "user", "content": "x" * 40}], threshold=5)
    assert not should_auto_compact([{"role": "user", "content": "short"}], threshold=100)

    messages = [
        {"role": "user", "content": "initial"},
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}],
        },
        {"role": "assistant", "content": "done"},
    ]
    assert recent_message_start(messages, keep_last=2) == 1


def test_compact_source_text_keeps_head_and_tail() -> None:
    original_head_chars = compact.SUMMARY_HEAD_CHARS
    original_tail_chars = compact.SUMMARY_TAIL_CHARS

    try:
        compact.SUMMARY_HEAD_CHARS = 80
        compact.SUMMARY_TAIL_CHARS = 80

        messages = [
            {"role": "user", "content": "HEAD-" + ("x" * 200) + "-TAIL"},
        ]
        text = compact_source_text(messages)
    finally:
        compact.SUMMARY_HEAD_CHARS = original_head_chars
        compact.SUMMARY_TAIL_CHARS = original_tail_chars

    assert "HEAD-" in text
    assert "...[middle omitted during compaction]..." in text
    assert "-TAIL" in text


def test_auto_compact_falls_back_when_summary_fails() -> None:
    class FailingRuntime:
        def call_llm_once(self, **kwargs):
            raise RuntimeError("offline failure")

    original_get_runtime = compact.get_runtime
    original_transcripts = compact.transcripts

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            compact.get_runtime = lambda: FailingRuntime()
            compact.transcripts = transcript.TranscriptStore(Path(tmpdir))

            messages = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
            compacted = auto_compact_messages(messages, keep_last=1)
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert compacted[0]["role"] == "user"
    assert str(Path(tmpdir)) in compacted[0]["content"]
    assert "Summary failed during compaction: offline failure" in compacted[0]["content"]
    assert compacted[-1] == {"role": "assistant", "content": "second"}


def run_all() -> None:
    test_micro_compact_text()
    test_auto_compact_helpers()
    test_compact_source_text_keeps_head_and_tail()
    test_auto_compact_falls_back_when_summary_fails()


if __name__ == "__main__":
    run_all()
    print("ok")
