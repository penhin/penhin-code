from __future__ import annotations

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
    assert manager.entries[-1]["type"] == "compaction"
    assert manager.build_context() == compacted


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
