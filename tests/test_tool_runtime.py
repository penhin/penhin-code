import io
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tool_runtime
from result import Result
from tool_runtime import (
    ApprovalFlow,
    CHILD_AGENT_POLICY,
    PARENT_AGENT_POLICY,
    PermissionPolicy,
    ToolRun,
    run_tool,
)


def test_tool_runtime_policy_and_control_signals() -> None:
    assert "compact" in PARENT_AGENT_POLICY.allow
    assert "task" in PARENT_AGENT_POLICY.allow
    assert "task" not in CHILD_AGENT_POLICY.allow

    approval = ApprovalFlow.preapproved({"workspace", "compact"})

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny={"workspace"})
    denied = run_tool("workspace", {}, policy)
    assert denied.result.exit_code == 1
    assert "Denied by policy" in denied.result.stderr

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny=set())
    workspace = run_tool("workspace", {}, policy)
    assert workspace.result.exit_code == 0
    assert workspace.manual_compact is False

    compact_run = run_tool("compact", {}, policy, approval)
    assert compact_run.result.exit_code == 0
    assert compact_run.manual_compact is True

    not_allowed = run_tool("read", {"path": "README.md"}, policy)
    assert not_allowed.result.exit_code == 1
    assert "Not allowed by policy" in not_allowed.result.stderr

    approval_required = run_tool(
        "write",
        {"path": "README.md", "content": "test"},
        PermissionPolicy(allow={"write"}, deny=set()),
        ApprovalFlow.require_confirmation({"write"}),
    )
    assert approval_required.result.exit_code == 1
    assert "Approval required" in approval_required.result.stderr


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
    assert result.result.exit_code == 1
    assert "[tool] start call_id=tool-" in output
    assert "name=workspace input=<none>" in output
    assert "status=error" in output
    assert "duration_ms=" in output
    assert "code=tool_error" in output
    assert "manual_compact=false" in output
    assert "approval_required=false" in output
    assert "stdout_chars=0" in output
    assert "stderr_chars=6" in output
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
    assert result.result.exit_code == 0
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
    assert result.result.exit_code == 1
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
    assert result.result.exit_code == 0
    assert "name=compact" in output
    assert "manual_compact=true" in output
    assert "approval_required=false" in output


def test_tool_runtime_reports_missing_required_input() -> None:
    result = run_tool(
        "read",
        {},
        PermissionPolicy(allow={"read"}, deny=set()),
    )

    assert result.result.exit_code == 1
    assert "Missing required input: path" in result.result.stderr
    assert result.result.meta["code"] == "invalid_tool_input"
    assert result.result.meta["missing"] == ["path"]


def run_all() -> None:
    test_tool_runtime_policy_and_control_signals()
    test_tool_runtime_input_summary_hides_sensitive_values()
    test_tool_runtime_logs_result_status()
    test_tool_runtime_logs_input_summary()
    test_tool_runtime_logs_blocked_access()
    test_tool_runtime_logs_manual_compact_flag()
    test_tool_runtime_reports_missing_required_input()


if __name__ == "__main__":
    run_all()
    print("ok")
