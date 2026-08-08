from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from penhin.agent.compaction import (
    CompactionCommitter,
    CompactionDecision,
    CompactionError,
    CompactionPolicy,
    chunk_compaction_messages,
    prepare_compaction,
    select_compaction_context,
)
from penhin.agent.context import RunContext
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from tests.helpers import ToolUseBlock


def snapshot_json(goal: str = "continue the task", next_step: str = "continue implementation") -> str:
    return json.dumps({
        "goal": goal,
        "completed": ["captured current progress"],
        "key_context": ["tests cover the behavior"],
        "constraints": [],
        "open_work": ["finish verification"],
        "tool_result_refs": [],
        "next_step": next_step,
    })


class SummaryRuntime:
    model = "summary-model"
    provider_id = "test-provider"
    context_window = 1_000
    compaction_reserve_tokens = 200

    def __init__(self, summary: str | None = None):
        self.summary = snapshot_json() if summary is None else summary
        self.kwargs = None

    def call_compact_once(self, **kwargs):
        self.kwargs = kwargs
        return self.summary


def forced_decision(*, tail_budget: int = 40) -> CompactionDecision:
    return CompactionDecision(
        watermark="normal",
        reason="forced",
        should_compact=True,
        current_tokens=500,
        context_window=1_000,
        compact_threshold=800,
        target_tokens=800,
        tail_budget_tokens=tail_budget,
        summary_max_tokens=128,
        chunk_budget_tokens=256,
        chunk_summary_max_tokens=64,
    )


def test_compaction_policy_is_a_pure_model_aware_decision() -> None:
    policy = CompactionPolicy(context_window=1_000, reserve_tokens=200)
    messages = [{"role": "user", "content": "x" * 3_300}]
    before = [dict(messages[0])]

    decision = policy.evaluate(messages)

    assert decision.watermark == "compact"
    assert decision.should_compact is True
    assert decision.compact_threshold == 800
    assert decision.target_tokens == 800
    assert decision.tail_budget_tokens == 200
    assert messages == before


def test_compaction_policy_scales_summary_and_chunk_budgets_with_model_window() -> None:
    small = CompactionPolicy(1_000, 200).evaluate([])
    large = CompactionPolicy(400_000, 16_384).evaluate([])

    assert small.summary_max_tokens == 100
    assert small.chunk_budget_tokens == 400
    assert large.summary_max_tokens == 4_096
    assert large.chunk_budget_tokens == 32_000
    assert large.tail_budget_tokens > small.tail_budget_tokens


def test_compact_watermark_levels() -> None:
    policy = CompactionPolicy(1_000, 200)
    assert policy.evaluate([{"role": "user", "content": "x" * 100}]).watermark == "normal"
    assert policy.evaluate([{"role": "user", "content": "x" * 2_500}]).watermark == "warning"
    assert policy.evaluate([{"role": "user", "content": "x" * 3_300}]).watermark == "compact"
    assert policy.evaluate([{"role": "user", "content": "x" * 4_100}]).watermark == "blocking"


def test_selector_uses_token_budget_and_keeps_tool_pair_atomic() -> None:
    messages = [
        {"role": "user", "content": "old" * 200},
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}],
        },
        {"role": "assistant", "content": "done"},
    ]

    selection = select_compaction_context(messages, tail_budget_tokens=25)

    assert selection.prefix == messages[:1]
    assert [message["role"] for message in selection.tail] == ["assistant", "user", "assistant"]
    assert selection.tail[0]["content"][0].id == "tool-1"
    assert selection.tail[1:] == messages[2:]
    assert selection.tail_tokens > 0


def test_selector_excludes_runtime_project_instructions() -> None:
    messages = [
        {"role": "user", "content": "<project_instructions>rules</project_instructions>"},
        {"role": "user", "content": "old conversation"},
        {"role": "assistant", "content": "recent"},
    ]

    selection = select_compaction_context(messages, tail_budget_tokens=10)

    assert all("project_instructions" not in str(message) for message in selection.prefix + selection.tail)


def test_chunker_keeps_tool_pair_together() -> None:
    messages = [
        {"role": "user", "content": "old" * 400},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "read"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}]},
        {"role": "assistant", "content": "done"},
    ]

    chunks = chunk_compaction_messages(messages, budget_tokens=200)

    assert any(
        [message["role"] for message in chunk[:2]] == ["assistant", "user"]
        for chunk in chunks
    )


def test_chunker_preserves_every_character_of_oversized_cjk_message() -> None:
    chunks = chunk_compaction_messages(
        [{"role": "user", "content": "中" * 1_000}],
        budget_tokens=100,
    )

    assert len(chunks) > 1
    assert sum(str(chunk[0]["content"]).count("中") for chunk in chunks) == 1_000


def test_prepare_compaction_summarizes_only_prefix_and_builds_artifact() -> None:
    runtime = SummaryRuntime()
    messages = [
        {"role": "user", "content": "OLD-SENTINEL " + "x" * 400},
        {"role": "assistant", "content": "old answer " + "y" * 200},
        {"role": "user", "content": "TAIL-SENTINEL"},
        {
            "role": "assistant",
            "content": "recent",
            "_meta": {"usage": {"context_tokens": 700}},
        },
    ]

    prepared = prepare_compaction(
        messages,
        forced_decision(tail_budget=30),
        hint="preserve filenames",
        runtime=runtime,
    )

    assert "OLD-SENTINEL" in runtime.kwargs["user_content"]
    assert "TAIL-SENTINEL" not in runtime.kwargs["user_content"]
    assert "preserve filenames" in runtime.kwargs["user_content"]
    assert prepared.messages[0]["content"].startswith("[Conversation compressed.]")
    assert prepared.messages[1:] == [
        {"role": "user", "content": "TAIL-SENTINEL"},
        {"role": "assistant", "content": "recent"},
    ]
    artifact = prepared.artifact.to_dict()
    assert artifact["version"] == 2
    assert artifact["snapshot"]["goal"] == "continue the task"
    assert artifact["model"] == "summary-model"
    assert artifact["provider"] == "test-provider"
    assert artifact["coveredMessageCount"] == 2
    assert len(artifact["sourceHash"]) == 64
    assert len(artifact["coveredHash"]) == 64
    assert artifact["tokenUsage"]["result"] < artifact["tokenUsage"]["source"]
    assert artifact["quality"]["validated"] is True
    assert artifact["quality"]["summaryTokens"] > 0
    assert 0 < artifact["quality"]["compressionRatio"] < 1


def test_prepare_compaction_records_recoverable_tool_result_references() -> None:
    result_content = json.dumps({
        "ok": True,
        "message": "file contents",
        "data": {"path": "src/app.py", "offset": 1, "next_offset": 201},
        "error": "",
        "meta": {"truncated": True},
    })
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "read"}]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_name": "read",
            "tool_use_id": "tool-1",
            "content": result_content,
        }]},
    ]

    prepared = prepare_compaction(
        messages,
        forced_decision(tail_budget=0),
        runtime=SummaryRuntime(),
    )

    reference = prepared.artifact.tool_result_refs[0]
    assert reference["toolUseId"] == "tool-1"
    assert reference["toolName"] == "read"
    assert reference["recovery"] == {
        "path": "src/app.py",
        "offset": 1,
        "next_offset": 201,
    }
    assert prepared.artifact.snapshot["tool_result_refs"] == [
        f"{reference['id']} | tool=read | path=src/app.py | offset=1 | next_offset=201"
    ]


def test_final_summary_rejects_invalid_structure() -> None:
    messages = [{"role": "user", "content": "important history"}]

    with pytest.raises(CompactionError, match="invalid JSON"):
        prepare_compaction(
            messages,
            forced_decision(),
            runtime=SummaryRuntime("plain text summary"),
        )

    assert messages == [{"role": "user", "content": "important history"}]


def test_final_summary_rejects_output_over_dynamic_budget() -> None:
    with pytest.raises(CompactionError, match="exceeded its token budget"):
        prepare_compaction(
            [{"role": "user", "content": "x" * 1_000}],
            forced_decision(),
            runtime=SummaryRuntime(snapshot_json(goal="z" * 2_000)),
        )


def test_forced_compaction_failure_emits_observer_event(monkeypatch) -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "important history"}],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    monkeypatch.setattr("penhin.runtime.runtime_manager.current", lambda: SummaryRuntime("not json"))

    with patch("penhin.evaluation.observer.emit") as emit:
        with pytest.raises(CompactionError):
            context.force_auto_compact("preserve details")

    failed = next(call for call in emit.call_args_list if call.args[0] == "context_compaction_failed")
    assert failed.kwargs["mode"] == "forced"
    assert failed.kwargs["has_hint"] is True
    assert failed.kwargs["duration_ms"] >= 0


def test_prepare_compaction_recursively_merges_chunk_summaries() -> None:
    class RecursiveRuntime(SummaryRuntime):
        def __init__(self):
            super().__init__()
            self.prompts = []

        def call_compact_once(self, **kwargs):
            self.prompts.append(kwargs["user_content"])
            if "Return only a JSON object" in kwargs["user_content"]:
                return snapshot_json(goal=f"summary-{len(self.prompts)}")
            return f"partial-{len(self.prompts)}"

    runtime = RecursiveRuntime()
    decision = replace(
        forced_decision(tail_budget=0),
        chunk_budget_tokens=100,
        chunk_summary_max_tokens=20,
    )
    messages = [
        {"role": "user", "content": f"chunk-{index} " + "x" * 300}
        for index in range(5)
    ]

    prepared = prepare_compaction(messages, decision, runtime=runtime)

    strategy = prepared.artifact.to_dict()["strategy"]
    assert strategy["chunkCount"] == 5
    assert strategy["summaryCalls"] == 6
    assert strategy["summaryLevels"] == 2
    assert strategy["recursive"] is True
    assert any("<partial_summary" in prompt for prompt in runtime.prompts)


def test_prepare_compaction_recompacts_when_first_result_misses_target() -> None:
    class TwoPassRuntime(SummaryRuntime):
        calls = 0

        def call_compact_once(self, **kwargs):
            self.calls += 1
            return snapshot_json(goal="z" * 260) if self.calls == 1 else snapshot_json()

    decision = replace(
        forced_decision(tail_budget=0),
        compact_threshold=100,
        target_tokens=100,
        chunk_budget_tokens=5_000,
    )

    prepared = prepare_compaction(
        [{"role": "user", "content": "x" * 4_000}],
        decision,
        runtime=TwoPassRuntime(),
    )

    assert prepared.artifact.passes == 2
    assert prepared.artifact.summary_calls == 2
    assert prepared.artifact.limit_reached is False
    assert prepared.artifact.result_tokens < 100


def test_prepare_compaction_stops_after_finite_pass_limit() -> None:
    class NonShrinkingSummaryRuntime(SummaryRuntime):
        def call_compact_once(self, **kwargs):
            return snapshot_json(goal="z" * 260)

    decision = replace(
        forced_decision(tail_budget=0),
        compact_threshold=100,
        target_tokens=100,
        chunk_budget_tokens=5_000,
    )

    prepared = prepare_compaction(
        [{"role": "user", "content": "x" * 4_000}],
        decision,
        runtime=NonShrinkingSummaryRuntime(),
    )

    assert prepared.artifact.passes == 3
    assert prepared.artifact.limit_reached is True
    assert prepared.artifact.result_tokens >= 100


def test_chunk_summary_failure_aborts_entire_preparation() -> None:
    class FailingChunkRuntime(SummaryRuntime):
        calls = 0

        def call_compact_once(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("chunk offline")
            return "partial"

    messages = [
        {"role": "user", "content": f"chunk-{index} " + "x" * 300}
        for index in range(3)
    ]
    original = [dict(message) for message in messages]
    decision = replace(
        forced_decision(tail_budget=0),
        chunk_budget_tokens=100,
        chunk_summary_max_tokens=20,
    )

    with pytest.raises(CompactionError, match="chunk 2/3 summary failed"):
        prepare_compaction(messages, decision, runtime=FailingChunkRuntime())

    assert messages == original


def test_prepare_compaction_failure_leaves_source_unchanged() -> None:
    class FailingRuntime(SummaryRuntime):
        def call_compact_once(self, **kwargs):
            raise RuntimeError("offline failure")

    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    original = [dict(message) for message in messages]

    with pytest.raises(CompactionError, match="offline failure"):
        prepare_compaction(messages, forced_decision(), runtime=FailingRuntime())

    assert messages == original


def test_committer_persists_before_mutating_live_context() -> None:
    class FailingSession:
        def append_compaction(self, artifact):
            raise OSError("disk full")

    prepared = prepare_compaction(
        [
            {"role": "user", "content": "old" * 100},
            {"role": "assistant", "content": "tail"},
        ],
        forced_decision(tail_budget=10),
        runtime=SummaryRuntime(),
    )
    live = [{"role": "user", "content": "unchanged"}]

    with pytest.raises(CompactionError, match="checkpoint commit failed"):
        CompactionCommitter.commit(live, prepared, FailingSession())

    assert live == [{"role": "user", "content": "unchanged"}]


def test_auto_compact_failure_does_not_append_session_event(monkeypatch) -> None:
    class RecordingSession:
        calls = 0

        def append_compaction(self, artifact):
            self.calls += 1

    def fail_compaction(*args, **kwargs):
        raise CompactionError("offline")

    run_context = RunContext(
        messages=[{"role": "user", "content": "x" * 5_000}],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
        session_manager=RecordingSession(),
    )
    monkeypatch.setattr("penhin.agent.context.prepare_compaction", fail_compaction)

    assert run_context.auto_compact_if_needed(1_000, 200) is False
    assert run_context.messages == [{"role": "user", "content": "x" * 5_000}]
    assert run_context.session_manager.calls == 0


def test_empty_summary_is_rejected() -> None:
    with pytest.raises(CompactionError, match="summary was empty"):
        prepare_compaction(
            [{"role": "user", "content": "hello"}],
            forced_decision(),
            runtime=SummaryRuntime(""),
        )
