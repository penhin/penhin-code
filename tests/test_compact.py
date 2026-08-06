import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.agent import compaction as compact
from penhin.agent.compaction import (
    CompactionError,
    auto_compact_messages,
    compact_watermark,
    compact_source_text,
    log_compact_watermark,
    recent_message_start,
    safe_recent_messages,
)
from penhin.agent.context import RunContext
from penhin.agent.projection import messages_for_api
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from tests.helpers import ToolUseBlock


def test_auto_compact_helpers() -> None:
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
    assert compact_watermark([{"role": "user", "content": "x" * 100}], 1000, 200) == "normal"
    assert compact_watermark([{"role": "user", "content": "x" * 2500}], 1000, 200) == "warning"
    assert compact_watermark([{"role": "user", "content": "x" * 3300}], 1000, 200) == "compact"
    assert compact_watermark([{"role": "user", "content": "x" * 4100}], 1000, 200) == "blocking"


def test_log_compact_watermark_returns_level() -> None:
    messages = [{"role": "user", "content": "x" * 2500}]

    assert log_compact_watermark(messages, 1000, 200) == "warning"


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


def test_auto_compact_raises_when_summary_fails() -> None:
    class FailingRuntime:
        def call_compact_once(self, **kwargs):
            raise RuntimeError("offline failure")

    original_get_runtime = compact.runtime_manager.current
    try:
        compact.runtime_manager.current = lambda: FailingRuntime()
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        with pytest.raises(CompactionError, match="offline failure"):
            auto_compact_messages(messages, keep_last=1)
    finally:
        compact.runtime_manager.current = original_get_runtime

    assert messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


def test_auto_compact_raises_when_compact_circuit_is_open() -> None:
    class CircuitOpenRuntime:
        def call_compact_once(self, **kwargs):
            raise RuntimeError("circuit open")

    original_get_runtime = compact.runtime_manager.current
    try:
        compact.runtime_manager.current = lambda: CircuitOpenRuntime()
        messages = [
            {"role": "user", "content": "head"},
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "tail"},
        ]
        with pytest.raises(CompactionError, match="circuit open"):
            auto_compact_messages(messages, keep_last=1)
    finally:
        compact.runtime_manager.current = original_get_runtime

    assert messages[-1] == {"role": "user", "content": "tail"}


def test_auto_compact_failure_does_not_append_session_event(monkeypatch) -> None:
    class RecordingSession:
        calls = 0

        def append_compaction(self, messages):
            self.calls += 1

    def fail_compaction(_messages):
        raise CompactionError("offline")

    run_context = RunContext(
        messages=[{"role": "user", "content": "x" * 5000}],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
        session_manager=RecordingSession(),
    )
    monkeypatch.setattr("penhin.agent.context.auto_compact_messages", fail_compaction)

    assert run_context.auto_compact_if_needed(1000, 200) is False
    assert run_context.messages == [{"role": "user", "content": "x" * 5000}]
    assert run_context.session_manager.calls == 0


def test_auto_compact_passes_hint_to_compact_runtime() -> None:
    class RecordingRuntime:
        kwargs = None

        def call_compact_once(self, **kwargs):
            self.kwargs = kwargs
            return "summary"

    recording_runtime = RecordingRuntime()
    original_get_runtime = compact.runtime_manager.current
    try:
        compact.runtime_manager.current = lambda: recording_runtime
        auto_compact_messages(
            [{"role": "user", "content": "remember cache"}],
            hint="preserve cache decisions",
        )
    finally:
        compact.runtime_manager.current = original_get_runtime

    assert "User-provided compact hint:" in recording_runtime.kwargs["user_content"]
    assert "preserve cache decisions" in recording_runtime.kwargs["user_content"]


def test_auto_compact_keeps_safe_tail_without_old_head() -> None:
    class SummaryRuntime:
        def call_compact_once(self, **kwargs):
            return "summary"

    original_get_runtime = compact.runtime_manager.current
    try:
        compact.runtime_manager.current = lambda: SummaryRuntime()
        messages = [
            {"role": "user", "content": "head-1"},
            {"role": "assistant", "content": "head-2"},
            {"role": "user", "content": "middle"},
            {"role": "assistant", "content": "tail-1"},
            {"role": "user", "content": "tail-2"},
        ]
        compacted = auto_compact_messages(messages, keep_last=2)
    finally:
        compact.runtime_manager.current = original_get_runtime

    assert compacted[1:] == [
        {"role": "assistant", "content": "tail-1"},
        {"role": "user", "content": "tail-2"},
    ]
    assert messages_for_api(compacted)[1:] == [
        {"role": "assistant", "content": "tail-1"},
        {"role": "user", "content": "tail-2"},
    ]


def test_auto_compact_does_not_duplicate_overlapping_head_and_tail() -> None:
    class SummaryRuntime:
        def call_compact_once(self, **kwargs):
            return "summary"

    original_get_runtime = compact.runtime_manager.current
    try:
        compact.runtime_manager.current = lambda: SummaryRuntime()
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        compacted = auto_compact_messages(messages, keep_last=2)
    finally:
        compact.runtime_manager.current = original_get_runtime

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
