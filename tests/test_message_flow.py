import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context import RunContext
from message_flow import (
    ToolCall,
    add_tool_result_cache_control,
    build_tool_execution_context,
    collect_tool_calls,
    execute_tool_blocks,
)
from result import Result
from tool_runtime import ApprovalFlow, PermissionPolicy, ToolRun


def test_add_tool_result_cache_control_marks_last_large_result() -> None:
    tool_results = [
        {"type": "tool_result", "content": "x" * 3000},
        {"type": "tool_result", "content": "short"},
        {"type": "tool_result", "content": "y" * 3000},
    ]

    add_tool_result_cache_control(tool_results)

    assert "cache_control" not in tool_results[0]
    assert "cache_control" not in tool_results[1]
    assert tool_results[2]["cache_control"] == {"type": "ephemeral"}


def test_add_tool_result_cache_control_skips_small_results() -> None:
    tool_results = [
        {"type": "tool_result", "content": "short"},
    ]

    add_tool_result_cache_control(tool_results)

    assert "cache_control" not in tool_results[0]


def test_collect_tool_calls_preserves_original_indexes_and_inputs() -> None:
    content = [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "workspace-1", "name": "workspace"},
    ]

    assert collect_tool_calls(content) == [
        ToolCall(index=1, tool_name="read", tool_input={"path": "a.py"}, tool_use_id="read-1"),
        ToolCall(index=2, tool_name="workspace", tool_input={}, tool_use_id="workspace-1"),
    ]


def test_collect_tool_calls_skips_non_list_content() -> None:
    assert collect_tool_calls("no tools") == []


def test_build_tool_execution_context_groups_shared_execution_inputs() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.preapproved(set()),
    )

    execution_context = build_tool_execution_context(
        context.policy,
        context.approval,
        context=context,
    )

    assert execution_context.policy is context.policy
    assert execution_context.approval is context.approval
    assert execution_context.approval_resolver is None
    assert execution_context.run_context is context


def test_delegation_guard_blocks_broad_tools_and_limits_reads() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"task", "search", "read"}, deny=set()),
        approval=ApprovalFlow.preapproved({"task", "search", "read"}),
    )
    content = [
        {"type": "tool_use", "id": "task-1", "name": "task", "input": {"task": "inspect"}},
        {"type": "tool_use", "id": "search-1", "name": "search", "input": {"query": "needle"}},
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "read-2", "name": "read", "input": {"path": "b.py"}},
        {"type": "tool_use", "id": "read-3", "name": "read", "input": {"path": "c.py"}},
        {"type": "tool_use", "id": "read-4", "name": "read", "input": {"path": "d.py"}},
    ]

    def fake_run_tool(tool_name, tool_input, policy, approval, context=None):
        return ToolRun(Result.success(f"{tool_name} ok"))

    with patch("message_flow.run_tool", side_effect=fake_run_tool) as mocked_run_tool:
        tool_results, manual_compact = execute_tool_blocks(
            content,
            build_tool_execution_context(
                context.policy,
                context.approval,
                context=context,
            ),
        )

    assert manual_compact is False
    assert [call.args[0] for call in mocked_run_tool.call_args_list] == ["task", "read", "read", "read"]
    assert '"code": "post_delegation_broad_tool_blocked"' in tool_results[1]["content"]
    assert '"code": "post_delegation_read_budget_exhausted"' in tool_results[5]["content"]
    assert context.post_delegation_read_budget == 0


def test_parallel_safe_tool_calls_preserve_result_order() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"read", "workspace"}, deny=set()),
        approval=ApprovalFlow.preapproved({"read", "workspace"}),
    )
    content = [
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "workspace-1", "name": "workspace", "input": {}},
        {"type": "tool_use", "id": "read-2", "name": "read", "input": {"path": "b.py"}},
    ]

    def fake_run_tool(tool_name, tool_input, policy, approval, context=None):
        return ToolRun(Result.success(f"{tool_name}:{tool_input.get('path', '')}"))

    with patch("message_flow.run_tool", side_effect=fake_run_tool):
        tool_results, manual_compact = execute_tool_blocks(
            content,
            build_tool_execution_context(
                context.policy,
                context.approval,
                context=context,
            ),
        )

    assert manual_compact is False
    assert [result["tool_use_id"] for result in tool_results] == ["read-1", "workspace-1", "read-2"]
    assert "read:a.py" in tool_results[0]["content"]
    assert "workspace:" in tool_results[1]["content"]
    assert "read:b.py" in tool_results[2]["content"]


def test_tool_execution_context_limits_total_tool_calls() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"read"}, deny=set()),
        approval=ApprovalFlow.preapproved({"read"}),
    )
    content = [
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "read-2", "name": "read", "input": {"path": "b.py"}},
        {"type": "tool_use", "id": "read-3", "name": "read", "input": {"path": "c.py"}},
    ]

    def fake_run_tool(tool_name, tool_input, policy, approval, context=None):
        return ToolRun(Result.success(f"{tool_name}:{tool_input.get('path', '')}"))

    execution_context = build_tool_execution_context(
        context.policy,
        context.approval,
        context=context,
        max_tool_calls=2,
    )
    with patch("message_flow.run_tool", side_effect=fake_run_tool) as mocked_run_tool:
        tool_results, manual_compact = execute_tool_blocks(content, execution_context)

    assert manual_compact is False
    assert [call.args[1]["path"] for call in mocked_run_tool.call_args_list] == ["a.py", "b.py"]
    assert [result["tool_use_id"] for result in tool_results] == ["read-1", "read-2", "read-3"]
    assert '"code": "tool_budget_exhausted"' in tool_results[2]["content"]
    assert execution_context.tool_calls_used == 2


def test_non_parallel_tool_splits_parallel_batches() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"read", "task"}, deny=set()),
        approval=ApprovalFlow.preapproved({"read", "task"}),
    )
    content = [
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "task-1", "name": "task", "input": {"task": "inspect"}},
        {"type": "tool_use", "id": "read-2", "name": "read", "input": {"path": "b.py"}},
    ]

    def fake_run_tool(tool_name, tool_input, policy, approval, context=None):
        return ToolRun(Result.success(f"{tool_name} ok"))

    with patch("message_flow.run_tool", side_effect=fake_run_tool) as mocked_run_tool:
        tool_results, manual_compact = execute_tool_blocks(
            content,
            build_tool_execution_context(
                context.policy,
                context.approval,
                context=context,
            ),
        )

    assert manual_compact is False
    assert [call.args[0] for call in mocked_run_tool.call_args_list] == ["read", "task", "read"]
    assert [result["tool_use_id"] for result in tool_results] == ["read-1", "task-1", "read-2"]


def test_human_message_resets_delegation_guard() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.preapproved(set()),
    )
    context.activate_post_delegation_guard("task")

    context.add_user_message("new request")

    assert context.post_delegation_read_budget is None
    assert context.post_delegation_source == ""


def run_all() -> None:
    test_add_tool_result_cache_control_marks_last_large_result()
    test_add_tool_result_cache_control_skips_small_results()
    test_collect_tool_calls_preserves_original_indexes_and_inputs()
    test_collect_tool_calls_skips_non_list_content()
    test_build_tool_execution_context_groups_shared_execution_inputs()
    test_delegation_guard_blocks_broad_tools_and_limits_reads()
    test_parallel_safe_tool_calls_preserve_result_order()
    test_tool_execution_context_limits_total_tool_calls()
    test_non_parallel_tool_splits_parallel_batches()
    test_human_message_resets_delegation_guard()


if __name__ == "__main__":
    run_all()
    print("ok")
