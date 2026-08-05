import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.agent.context import RunContext
from penhin.tools.execution import ApprovalFlow, PermissionPolicy


def empty_policy() -> PermissionPolicy:
    return PermissionPolicy(allow=set(), deny=set())


def test_run_context_adds_messages() -> None:
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    context.add_user_message("hello")
    context.add_assistant_message([{"type": "text", "text": "done"}])
    context.add_tool_results([{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}])

    assert context.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}],
        },
    ]


def test_run_context_stores_session_path() -> None:
    session_path = Path(".penhin/sessions/session_test.jsonl")
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
        session_path=session_path,
    )

    assert context.session_path == session_path


def test_post_delegation_guard_blocks_bypass_tools() -> None:
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    context.activate_post_delegation_guard("task")

    bash_block = context.post_delegation_tool_block("bash")
    compact_block = context.post_delegation_tool_block("compact")
    workspace_block = context.post_delegation_tool_block("workspace")
    task_start_block = context.post_delegation_tool_block("task_start")
    todo_set_block = context.post_delegation_tool_block("todo_set")

    assert bash_block is not None
    assert bash_block.meta["code"] == "post_delegation_broad_tool_blocked"
    assert "Do not call more tools" in bash_block.error
    assert compact_block is not None
    assert compact_block.meta["code"] == "post_delegation_broad_tool_blocked"
    assert workspace_block is not None
    assert workspace_block.meta["code"] == "post_delegation_broad_tool_blocked"
    assert task_start_block is not None
    assert task_start_block.meta["code"] == "post_delegation_broad_tool_blocked"
    assert todo_set_block is not None
    assert todo_set_block.meta["code"] == "post_delegation_broad_tool_blocked"
