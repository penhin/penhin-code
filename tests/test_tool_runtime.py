import io
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tool_runtime
from context import RunContext
from result import Result
from tool_runtime import (
    ApprovalFlow,
    PARENT_AGENT_POLICY,
    PermissionPolicy,
    ToolRun,
    run_tool,
    tool_names_for,
)


def test_tool_runtime_policy_and_control_signals() -> None:
    assert "compact" in PARENT_AGENT_POLICY.allow
    assert "snip" in PARENT_AGENT_POLICY.allow
    assert "task" in PARENT_AGENT_POLICY.allow
    assert "task" not in tool_names_for("child")

    approval = ApprovalFlow.preapproved({"workspace", "compact"})

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny={"workspace"})
    denied = run_tool("workspace", {}, policy)
    assert denied.result.ok is False
    assert "Denied by policy" in denied.result.error

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny=set())
    workspace = run_tool("workspace", {}, policy)
    assert workspace.result.ok is True
    assert workspace.manual_compact is False

    compact_run = run_tool("compact", {}, policy, approval)
    assert compact_run.result.ok is True
    assert compact_run.manual_compact is True

    not_allowed = run_tool("read", {"path": "README.md"}, policy)
    assert not_allowed.result.ok is False
    assert "Not allowed by policy" in not_allowed.result.error

    approval_required = run_tool(
        "write",
        {"path": "README.md", "content": "test"},
        PermissionPolicy(allow={"write"}, deny=set()),
        ApprovalFlow.require_confirmation({"write"}),
    )
    assert approval_required.result.ok is False
    assert "Approval required" in approval_required.result.error


def empty_context() -> RunContext:
    return RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )


def test_snip_tool_marks_selected_turns() -> None:
    context = empty_context()
    context.messages[:] = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second request"},
    ]

    result = run_tool(
        "snip",
        {"selectors": ["1"]},
        PermissionPolicy(allow={"snip"}, deny=set()),
        ApprovalFlow.preapproved({"snip"}),
        context=context,
    )

    assert result.result.ok is True
    assert result.result.meta["snipped"] == 2
    assert context.messages[0]["_meta"]["snipped"] is True
    assert context.messages[1]["_meta"]["snipped"] is True
    assert "_meta" not in context.messages[2]


def test_snip_tool_requires_context() -> None:
    result = run_tool(
        "snip",
        {"selectors": ["1"]},
        PermissionPolicy(allow={"snip"}, deny=set()),
        ApprovalFlow.preapproved({"snip"}),
    )

    assert result.result.ok is False
    assert result.result.meta["code"] == "missing_context"


def test_runtime_permission_setup_for_parent_modes() -> None:
    default_policy, default_approval = tool_runtime.runtime_permission_setup("default")
    assert "write" in default_policy.allow
    assert not default_approval.is_approved("write", {"path": "demo.txt", "content": "hello"})

    auto_policy, auto_approval = tool_runtime.runtime_permission_setup("auto-review")
    assert "read" in auto_policy.allow
    assert "task_show" in auto_policy.allow
    assert "write" in auto_policy.deny
    assert "bash" in auto_policy.deny
    assert not auto_approval.is_approved("write", {"path": "demo.txt", "content": "hello"})

    full_policy, full_approval = tool_runtime.runtime_permission_setup("full-access")
    assert "write" in full_policy.allow
    assert full_approval.is_approved("write", {"path": "demo.txt", "content": "hello"})


def test_bash_prefix_approval_allows_matching_commands() -> None:
    approval = ApprovalFlow.require_confirmation({"bash"})
    approval.approve_rule("bash", "pytest:*")

    assert approval.is_approved("bash", {"command": "pytest tests/test_agent.py -q"})
    assert approval.is_approved("bash", {"command": "pytest -q"})
    assert not approval.is_approved("bash", {"command": "pytest -q && rm -rf ."})
    assert not approval.is_approved("bash", {"command": "python -m pytest -q"})


def test_tool_runtime_input_summary_hides_sensitive_values() -> None:
    summary = tool_runtime.input_summary(
        {
            "path": "agent.py",
            "content": "secret content",
            "command": "echo secret",
            "unknown": {"hidden": True},
        }
    )

    assert 'path="agent.py"' in summary
    assert 'content_sha="' in summary
    assert "content_chars=14" in summary
    assert 'command_sha="' in summary
    assert "command_chars=11" in summary
    assert "unknown=<hidden:dict>" in summary
    assert "secret content" not in summary
    assert "echo secret" not in summary
    assert "True" not in summary


def capture_tool_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("penhin.tool_runtime")
    original_level = logger.level
    original_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return stream, handler, logger, original_level, original_propagate


def restore_tool_logs(handler, logger, original_level, original_propagate) -> None:
    logger.removeHandler(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_tool_runtime_logs_result_status() -> None:
    stream, handler, logger, original_level, original_propagate = capture_tool_logs()
    try:
        with patch.object(
            tool_runtime,
            "execute_tool",
            return_value=ToolRun(Result.failure("broken", code="tool_error")),
        ):
            result = run_tool("workspace", {}, PermissionPolicy(allow={"workspace"}, deny=set()))
    finally:
        restore_tool_logs(handler, logger, original_level, original_propagate)

    output = stream.getvalue()
    assert result.result.ok is False
    assert "[tool] start call_id=tool-" in output
    assert "name=workspace input=<none>" in output
    assert "status=error" in output
    assert "duration_ms=" in output
    assert "code=tool_error" in output
    assert "manual_compact=false" in output
    assert "approval_required=false" in output
    assert "message_chars=0" in output
    assert "error_chars=6" in output
    assert 'data_type="none"' in output
    assert 'meta_keys=["code"]' in output


def test_tool_runtime_logs_input_summary() -> None:
    stream, handler, logger, original_level, original_propagate = capture_tool_logs()
    try:
        with patch.object(
            tool_runtime,
            "execute_tool",
            return_value=ToolRun(Result.success("ok")),
        ):
            result = run_tool(
                "write",
                {"path": "agent.py", "content": "secret content"},
                PermissionPolicy(allow={"write"}, deny=set()),
                ApprovalFlow.preapproved({"write"}),
            )
    finally:
        restore_tool_logs(handler, logger, original_level, original_propagate)

    output = stream.getvalue()
    assert result.result.ok is True
    assert "[tool] start call_id=tool-" in output
    assert "status=ok" in output
    assert "manual_compact=false" in output
    assert "approval_required=false" in output
    assert 'input=content_sha="' in output
    assert "content_chars=14" in output
    assert 'path="agent.py"' in output
    assert "secret content" not in output


def test_tool_runtime_logs_blocked_access() -> None:
    stream, handler, logger, original_level, original_propagate = capture_tool_logs()
    try:
        result = run_tool(
            "write",
            {"path": "agent.py", "content": "secret content"},
            PermissionPolicy(allow={"write"}, deny=set()),
            ApprovalFlow.require_confirmation({"write"}),
        )
    finally:
        restore_tool_logs(handler, logger, original_level, original_propagate)

    output = stream.getvalue()
    assert result.approval_required is True
    assert result.result.ok is False
    assert "[tool] blocked call_id=tool-" in output
    assert "name=write" in output
    assert "status=approval_required" in output
    assert "duration_ms=" in output
    assert "code=tool_approval_required" in output
    assert 'path="agent.py"' in output
    assert 'content_sha="' in output
    assert "content_chars=14" in output
    assert "secret content" not in output


def test_tool_runtime_logs_manual_compact_flag() -> None:
    stream, handler, logger, original_level, original_propagate = capture_tool_logs()
    try:
        result = run_tool(
            "compact",
            {},
            PermissionPolicy(allow={"compact"}, deny=set()),
            ApprovalFlow.preapproved({"compact"}),
        )
    finally:
        restore_tool_logs(handler, logger, original_level, original_propagate)

    output = stream.getvalue()
    assert result.manual_compact is True
    assert result.result.ok is True
    assert "name=compact" in output
    assert "manual_compact=true" in output
    assert "approval_required=false" in output


def test_tool_runtime_reports_missing_required_input() -> None:
    result = run_tool(
        "read",
        {},
        PermissionPolicy(allow={"read"}, deny=set()),
    )

    assert result.result.ok is False
    assert "Missing required input: path" in result.result.error
    assert result.result.meta["code"] == "invalid_tool_input"
    assert result.result.meta["missing"] == ["path"]


def test_tool_runtime_reports_invalid_input_type() -> None:
    path_result = run_tool(
        "read",
        {"path": 123},
        PermissionPolicy(allow={"read"}, deny=set()),
    )

    assert path_result.result.ok is False
    assert "Invalid input type: path expected string" in path_result.result.error
    assert path_result.result.meta["code"] == "invalid_tool_input"
    assert path_result.result.meta["field"] == "path"
    assert path_result.result.meta["expected"] == "string"
    assert path_result.result.meta["actual"] == "int"

    limit_result = run_tool(
        "read",
        {"path": "README.md", "limit": True},
        PermissionPolicy(allow={"read"}, deny=set()),
    )

    assert limit_result.result.ok is False
    assert "Invalid input type: limit expected integer" in limit_result.result.error
    assert limit_result.result.meta["code"] == "invalid_tool_input"
    assert limit_result.result.meta["field"] == "limit"
    assert limit_result.result.meta["expected"] == "integer"
    assert limit_result.result.meta["actual"] == "bool"


def test_tool_runtime_reports_invalid_enum_value() -> None:
    result = run_tool(
        "task",
        {"task": "verify work", "agent_type": "verification"},
        PermissionPolicy(allow={"task"}, deny=set()),
    )

    assert result.result.ok is False
    assert "Invalid input value: agent_type must be one of explore, general, plan" in result.result.error
    assert result.result.meta["code"] == "invalid_tool_input"
    assert result.result.meta["field"] == "agent_type"
    assert result.result.meta["expected"] == "enum"
    assert result.result.meta["allowed"] == ["explore", "general", "plan"]
    assert result.result.meta["actual"] == "verification"


def test_tool_runtime_logs_unknown_input_fields_without_blocking() -> None:
    stream, handler, logger, original_level, original_propagate = capture_tool_logs()
    try:
        with patch.object(
            tool_runtime,
            "execute_tool",
            return_value=ToolRun(Result.success("ok")),
        ):
            result = run_tool(
                "read",
                {"path": "README.md", "extra": "value"},
                PermissionPolicy(allow={"read"}, deny=set()),
            )
    finally:
        restore_tool_logs(handler, logger, original_level, original_propagate)

    output = stream.getvalue()
    assert result.result.ok is True
    assert "[tool] unknown_input call_id=tool-" in output
    assert "name=read" in output
    assert 'fields=["extra"]' in output
    assert "status=ok" in output
    assert "extra=<hidden:str>" in output
    assert "value" not in output


def run_all() -> None:
    test_tool_runtime_policy_and_control_signals()
    test_snip_tool_marks_selected_turns()
    test_snip_tool_requires_context()
    test_runtime_permission_setup_for_parent_modes()
    test_tool_runtime_input_summary_hides_sensitive_values()
    test_tool_runtime_logs_result_status()
    test_tool_runtime_logs_input_summary()
    test_tool_runtime_logs_blocked_access()
    test_tool_runtime_logs_manual_compact_flag()
    test_tool_runtime_reports_missing_required_input()
    test_tool_runtime_reports_invalid_input_type()
    test_tool_runtime_reports_invalid_enum_value()
    test_tool_runtime_logs_unknown_input_fields_without_blocking()


if __name__ == "__main__":
    run_all()
    print("ok")
