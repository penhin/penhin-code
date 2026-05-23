import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
from result import Result
from tool_runtime import ApprovalFlow, ToolRun


def test_resolve_approval_approves_for_session() -> None:
    approval = ApprovalFlow.require_confirmation({"write"})
    tool_input = {"path": "demo.txt", "content": "hello"}

    with patch("builtins.input", return_value="ys"), patch("agent.run_tool") as mocked_run_tool:
        mocked_run_tool.return_value = ToolRun(Result.success("ok"))
        tool_run = agent.resolve_approval("write", tool_input, approval)

    assert tool_run.result.stdout == "ok"
    assert approval.is_approved("write", tool_input)
    mocked_run_tool.assert_called_once()


def run_all() -> None:
    test_resolve_approval_approves_for_session()


if __name__ == "__main__":
    run_all()
    print("ok")
