from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from penhin.agent.session_manager import SessionManager
from penhin.agent.session_manager import SessionFormatError
from penhin.agent.context import RunContext
from penhin.tools.execution import ApprovalFlow, PermissionPolicy


def message(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_session_appends_without_rewriting_existing_bytes(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path, [message("user", "first")])
    before = manager.path.read_bytes()

    manager.append_message(message("assistant", "second"))
    after = manager.path.read_bytes()

    assert after.startswith(before)
    assert len(after) > len(before)
    items = [json.loads(line) for line in after.decode().splitlines()]
    assert items[2]["parentId"] == items[1]["id"]


def test_run_context_appends_stable_messages_during_a_turn(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set()),
        approval=ApprovalFlow.require_confirmation(set()),
        session_path=manager.path,
        session_manager=manager,
    )

    context.add_user_message("hello")
    context.add_assistant_message("done")

    reopened = SessionManager.open(manager.path)
    assert reopened.build_context() == [message("user", "hello"), message("assistant", "done")]


def test_branching_keeps_both_children_and_projects_active_path(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    root = manager.append_message(message("user", "question"))
    original = manager.append_message(message("assistant", "original"))

    manager.branch(root)
    alternate = manager.append_message(message("assistant", "alternate"))

    assert manager.build_context() == [
        message("user", "question"),
        message("assistant", "alternate"),
    ]
    assert {entry["id"] for entry in manager.children(root)} == {original, alternate}
    assert any("original" in line for line in manager.render_tree())
    assert any("alternate" in line and line.endswith(" *") for line in manager.render_tree())

    reopened = SessionManager.open(manager.path)
    assert reopened.leaf_id == alternate
    assert reopened.build_context() == manager.build_context()


def test_context_rewrite_is_an_append_only_compaction_checkpoint(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path, [
        message("user", "old"),
        message("assistant", "answer"),
    ])
    before = manager.path.read_bytes()
    compacted = [message("user", "summary"), message("assistant", "tail")]

    manager.sync_messages(compacted)

    assert manager.path.read_bytes().startswith(before)
    assert manager.entries[-1]["type"] == "context_rewrite"
    assert manager.build_context() == compacted


def test_structured_compaction_checkpoint_records_coverage_and_rebuilds_context(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    first = manager.append_message(message("user", "old question"))
    second = manager.append_message(message("assistant", "old answer"))
    tail = manager.append_message(message("user", "recent detail"))
    before = manager.path.read_bytes()

    checkpoint = manager.append_compaction({
        "version": 1,
        "summary": "goal and completed work",
        "retainedMessages": [message("user", "recent detail")],
        "coveredMessageCount": 2,
        "sourceMessageCount": 3,
        "sourceHash": "source-hash",
        "coveredHash": "covered-hash",
        "reason": "automatic",
        "tokenUsage": {"source": 100, "result": 20},
    })

    entry = manager.get_entry(checkpoint)
    artifact = entry["artifact"]
    assert manager.path.read_bytes().startswith(before)
    assert artifact["sourceLeafId"] == tail
    assert artifact["coveredThroughEntryId"] == second
    assert artifact["retainedSourceEntryIds"] == [tail]
    assert manager.build_context() == [
        message("user", "[Conversation compressed.]\n\ngoal and completed work"),
        message("user", "recent detail"),
    ]
    assert first != second


def test_branching_before_structured_checkpoint_preserves_original_history(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    root = manager.append_message(message("user", "question"))
    answer = manager.append_message(message("assistant", "answer"))
    checkpoint = manager.append_compaction({
        "version": 1,
        "summary": "question was answered",
        "retainedMessages": [],
        "coveredMessageCount": 2,
        "sourceMessageCount": 2,
    })

    manager.branch(answer)
    alternate = manager.append_message(message("assistant", "alternate"))

    assert manager.build_context() == [message("user", "question"), message("assistant", "answer"), message("assistant", "alternate")]
    assert {entry["id"] for entry in manager.children(answer)} == {checkpoint, alternate}
    manager.branch(checkpoint)
    assert manager.build_context() == [message("user", "[Conversation compressed.]\n\nquestion was answered")]
    assert root != answer


def test_run_context_compaction_round_trips_structured_artifact(tmp_path: Path, monkeypatch) -> None:
    class Runtime:
        model = "compact-model"
        provider_id = "test-provider"
        context_window = 1_000
        compaction_reserve_tokens = 200

        def call_compact_once(self, **kwargs):
            return json.dumps({
                "goal": "persist the session",
                "completed": ["compressed old history"],
                "key_context": ["recent detail remains"],
                "constraints": [],
                "open_work": [],
                "tool_result_refs": [],
                "next_step": "continue the session",
            })

    manager = SessionManager.create(tmp_path, [
        message("user", "old question " + "x" * 500),
        message("assistant", "old answer " + "y" * 500),
        message("user", "recent detail"),
    ])
    context = RunContext(
        messages=manager.build_context(),
        policy=PermissionPolicy(allow=set()),
        approval=ApprovalFlow.require_confirmation(set()),
        session_path=manager.path,
        session_manager=manager,
    )
    monkeypatch.setattr("penhin.runtime.runtime_manager.current", lambda: Runtime())

    context.force_auto_compact("keep the recent detail")

    entry = manager.entries[-1]
    assert entry["type"] == "compaction"
    assert entry["artifact"]["version"] == 2
    assert entry["artifact"]["model"] == "compact-model"
    assert entry["artifact"]["coveredThroughEntryId"] is not None
    assert entry["artifact"]["strategy"]["passes"] == 1
    assert entry["artifact"]["strategy"]["summaryCalls"] == 1
    assert SessionManager.open(manager.path).build_context() == context.messages


def test_later_checkpoint_replaces_earlier_checkpoint_on_same_branch(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path, [
        message("user", "question"),
        message("assistant", "answer"),
    ])
    first_checkpoint = manager.append_compaction({
        "version": 1,
        "summary": "first summary",
        "retainedMessages": [],
        "coveredMessageCount": 2,
        "sourceMessageCount": 2,
    })
    recent = manager.append_message(message("user", "new detail"))

    second_checkpoint = manager.append_compaction({
        "version": 1,
        "summary": "updated summary",
        "retainedMessages": [message("user", "new detail")],
        "coveredMessageCount": 1,
        "sourceMessageCount": 2,
    })

    artifact = manager.get_entry(second_checkpoint)["artifact"]
    assert artifact["coveredThroughEntryId"] == first_checkpoint
    assert artifact["retainedSourceEntryIds"] == [recent]
    assert manager.build_context() == [
        message("user", "[Conversation compressed.]\n\nupdated summary"),
        message("user", "new detail"),
    ]


def test_invalid_structured_checkpoint_is_ignored_during_rebuild(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path, [message("user", "original")])
    manager.append_entry("compaction", artifact={
        "version": 1,
        "summary": "corrupt coverage",
        "retainedMessages": [],
        "coveredMessageCount": 99,
        "sourceMessageCount": 1,
    })
    manager.append_message(message("assistant", "still on original history"))

    assert SessionManager.open(manager.path).build_context() == [
        message("user", "original"),
        message("assistant", "still on original history"),
    ]


def test_compaction_tool_result_reference_recovers_original_block(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    manager.append_message({
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tool-1", "name": "read"}],
    })
    raw_content = json.dumps({"ok": True, "data": {"path": "src/app.py"}})
    result_block = {
        "type": "tool_result",
        "tool_name": "read",
        "tool_use_id": "tool-1",
        "content": raw_content,
    }
    manager.append_message({"role": "user", "content": [result_block]})
    digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    reference_id = f"tool-result:tool-1:{digest[:12]}"
    snapshot = {
        "goal": "continue",
        "completed": [],
        "key_context": [],
        "constraints": [],
        "open_work": [],
        "tool_result_refs": [reference_id],
        "next_step": "continue",
    }
    checkpoint = manager.append_compaction({
        "version": 2,
        "summary": json.dumps(snapshot),
        "snapshot": snapshot,
        "retainedMessages": [],
        "coveredMessageCount": 2,
        "sourceMessageCount": 2,
        "toolResultRefs": [{
            "id": reference_id,
            "toolUseId": "tool-1",
            "toolName": "read",
            "messageIndex": 1,
            "contentHash": digest,
            "contentChars": len(raw_content),
            "recovery": {"path": "src/app.py"},
        }],
    })

    reopened = SessionManager.open(manager.path)
    assert reopened.get_entry(checkpoint)["artifact"]["toolResultRefs"][0]["sourceEntryId"]
    assert reopened.recover_tool_result(checkpoint, reference_id) == result_block


def test_consecutive_compactions_follow_current_model_and_remain_reopenable(tmp_path: Path, monkeypatch) -> None:
    class Runtime:
        provider_id = "test-provider"
        context_window = 1_000
        compaction_reserve_tokens = 200
        model = "model-a"

        def call_compact_once(self, **kwargs):
            return json.dumps({
                "goal": "continue",
                "completed": [],
                "key_context": [],
                "constraints": [],
                "open_work": [],
                "tool_result_refs": [],
                "next_step": "continue",
            })

    runtime = Runtime()
    manager = SessionManager.create(tmp_path, [message("user", "initial history")])
    context = RunContext(
        messages=manager.build_context(),
        policy=PermissionPolicy(allow=set()),
        approval=ApprovalFlow.require_confirmation(set()),
        session_manager=manager,
    )
    monkeypatch.setattr("penhin.runtime.runtime_manager.current", lambda: runtime)

    context.force_auto_compact()
    first_checkpoint = manager.entries[-1]["id"]
    context.add_user_message("new work")
    runtime.model = "model-b"
    context.force_auto_compact()

    second = manager.entries[-1]
    assert second["artifact"]["model"] == "model-b"
    assert second["artifact"]["coveredThroughEntryId"] == first_checkpoint
    assert SessionManager.open(manager.path).build_context() == context.messages


def test_project_instructions_are_runtime_context_not_session_history(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    manager.sync_messages([
        message("user", "<project_instructions>\nlocal rules\n</project_instructions>"),
        message("user", "actual question"),
    ])

    assert manager.build_context() == [message("user", "actual question")]


def test_message_only_jsonl_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid_session.jsonl"
    path.write_text(
        json.dumps(message("user", "hello")) + "\n" +
        json.dumps(message("assistant", "world")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SessionFormatError, match="Invalid session header"):
        SessionManager.open(path)


def test_fork_is_self_contained_and_records_parent_session(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    root = manager.append_message(message("user", "question"))
    manager.append_message(message("assistant", "discarded"))

    forked = manager.fork(tmp_path, root)

    assert forked.header["parentSession"] == str(manager.path)
    assert forked.build_context() == [message("user", "question")]
    forked.append_message(message("assistant", "new answer"))
    assert manager.build_context()[-1]["content"] == "discarded"
