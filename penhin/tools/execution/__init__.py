"""Tool validation, authorization, and execution."""

from .approval import (
    ApprovalFlow, PARENT_AGENT_POLICY, PermissionPolicy, approval_key,
    runtime_permission_setup, tool_names_for,
)
from .service import ToolRun, execute_tool, run_tool


class ToolExecutor:
    """Stable execution boundary used by agents and orchestration workers."""

    def execute(self, tool_name, tool_input, policy, approval=None, context=None) -> ToolRun:
        return run_tool(tool_name, tool_input, policy, approval, context)


__all__ = [
    "ApprovalFlow", "PARENT_AGENT_POLICY", "PermissionPolicy", "ToolExecutor", "ToolRun",
    "approval_key", "execute_tool", "run_tool", "runtime_permission_setup", "tool_names_for",
]
