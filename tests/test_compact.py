import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compact
import transcript
from circuit_breaker import CircuitBreakerOpen
from compact import (
    auto_compact_messages,
    compact_watermark,
    compact_source_text,
    log_compact_watermark,
    micro_compact_if_needed,
    micro_compact_text,
    recent_message_start,
    safe_recent_messages,
    should_auto_compact,
)

from tests.helpers import ToolUseBlock
from message_projection import messages_for_api


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

    projected = micro_compact_text(messages, keep_recent=2)

    results = messages[1]["content"]
    assert results[0]["content"] == long_output
    projected_results = projected[1]["content"]
    assert projected_results[0]["content"].startswith("[collapsed tool_result list;")
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
    todo_projected = micro_compact_text(todo_messages, keep_recent=0)
    assert todo_messages[1]["content"][0]["content"] == long_output
    assert todo_projected[1]["content"][0]["content"] == long_output


def test_auto_compact_helpers() -> None:
    assert should_auto_compact([{"role": "user", "content": "x" * 40}], threshold=5)
    assert not should_auto_compact([{"role": "user", "content": "short"}], threshold=100)
    assert compact.THRESHOLD == compact.COMPACT_THRESHOLD

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


def test_compact_watermark_levels() -> None:
    assert compact_watermark([{"role": "user", "content": "x" * 100}]) == "normal"
    assert compact_watermark([{"role": "user", "content": "x" * compact.WARNING_THRESHOLD * 4}]) == "warning"
    assert compact_watermark([{"role": "user", "content": "x" * compact.COMPACT_THRESHOLD * 4}]) == "compact"
    assert compact_watermark([{"role": "user", "content": "x" * compact.BLOCKING_THRESHOLD * 4}]) == "blocking"


def test_log_compact_watermark_returns_level() -> None:
    messages = [{"role": "user", "content": "x" * compact.WARNING_THRESHOLD * 4}]

    assert log_compact_watermark(messages) == "warning"


def test_micro_compact_if_needed_uses_limit() -> None:
    old_output = "x" * 400
    recent_output = "z" * 400
    messages = [
        {
            "role": "assistant",
            "content": [
                ToolUseBlock("tool-1", "bash"),
                ToolUseBlock("tool-2", "search"),
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": old_output},
                {"type": "tool_result", "tool_use_id": "tool-2", "content": recent_output},
            ],
        },
    ]

    micro_compact_if_needed(messages, limit=100000)
    assert messages[1]["content"][0]["content"] == old_output

    micro_compact_if_needed(messages, limit=100)
    assert messages[1]["content"][0]["content"] == old_output
    projected = messages_for_api(messages, collapse_keep_recent=1)
    assert projected[1]["content"][0]["content"].startswith("[collapsed tool_result bash;")
    assert messages[1]["content"][1]["content"] == recent_output


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
        def call_compact_once(self, **kwargs):
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
    assert len(compacted) == 2


def test_auto_compact_continues_when_compact_circuit_is_open() -> None:
    class CircuitOpenRuntime:
        def call_compact_once(self, **kwargs):
            raise CircuitBreakerOpen("open")

    original_get_runtime = compact.get_runtime
    original_transcripts = compact.transcripts

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            compact.get_runtime = lambda: CircuitOpenRuntime()
            compact.transcripts = transcript.TranscriptStore(Path(tmpdir))

            messages = [
                {"role": "user", "content": "head"},
                {"role": "assistant", "content": "old"},
                {"role": "user", "content": "tail"},
            ]
            compacted = auto_compact_messages(messages, keep_head=1, keep_last=1)
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert "Summary skipped during compaction" in compacted[0]["content"]
    assert "compact circuit breaker is open" in compacted[0]["content"]
    assert messages_for_api(compacted)[-1] == {"role": "user", "content": "tail"}
    assert len(compacted) == 2


def test_auto_compact_passes_hint_to_compact_runtime() -> None:
    class RecordingRuntime:
        kwargs = None

        def call_compact_once(self, **kwargs):
            self.kwargs = kwargs
            return "summary"

    recording_runtime = RecordingRuntime()
    original_get_runtime = compact.get_runtime
    original_transcripts = compact.transcripts

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            compact.get_runtime = lambda: recording_runtime
            compact.transcripts = transcript.TranscriptStore(Path(tmpdir))

            auto_compact_messages(
                [{"role": "user", "content": "remember cache"}],
                hint="preserve cache decisions",
            )
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert "User-provided compact hint:" in recording_runtime.kwargs["user_content"]
    assert "preserve cache decisions" in recording_runtime.kwargs["user_content"]


def test_auto_compact_keeps_safe_tail_without_old_head() -> None:
    class FailingRuntime:
        def call_compact_once(self, **kwargs):
            raise RuntimeError("offline failure")

    original_get_runtime = compact.get_runtime
    original_transcripts = compact.transcripts

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            compact.get_runtime = lambda: FailingRuntime()
            compact.transcripts = transcript.TranscriptStore(Path(tmpdir))

            messages = [
                {"role": "user", "content": "head-1"},
                {"role": "assistant", "content": "head-2"},
                {"role": "user", "content": "middle"},
                {"role": "assistant", "content": "tail-1"},
                {"role": "user", "content": "tail-2"},
            ]
            compacted = auto_compact_messages(messages, keep_head=2, keep_last=2)
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert compacted[1:] == [
        {"role": "assistant", "content": "tail-1"},
        {"role": "user", "content": "tail-2"},
    ]
    assert messages_for_api(compacted)[1:] == [
        {"role": "assistant", "content": "tail-1"},
        {"role": "user", "content": "tail-2"},
    ]


def test_auto_compact_does_not_duplicate_overlapping_head_and_tail() -> None:
    class FailingRuntime:
        def call_compact_once(self, **kwargs):
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
            compacted = auto_compact_messages(messages, keep_head=2, keep_last=2)
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert compacted[1:] == messages


def test_safe_recent_messages_preserves_tool_use_result_pair() -> None:
    messages = [
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "workspace")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}],
        },
        {"role": "assistant", "content": "done"},
    ]

    assert safe_recent_messages(messages, keep_last=2) == messages[1:]


def run_all() -> None:
    test_micro_compact_text()
    test_auto_compact_helpers()
    test_compact_watermark_levels()
    test_log_compact_watermark_returns_level()
    test_micro_compact_if_needed_uses_limit()
    test_compact_source_text_keeps_head_and_tail()
    test_auto_compact_falls_back_when_summary_fails()
    test_auto_compact_continues_when_compact_circuit_is_open()
    test_auto_compact_passes_hint_to_compact_runtime()
    test_auto_compact_keeps_safe_tail_without_old_head()
    test_auto_compact_does_not_duplicate_overlapping_head_and_tail()
    test_safe_recent_messages_preserves_tool_use_result_pair()


if __name__ == "__main__":
    run_all()
    print("ok")
