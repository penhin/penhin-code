"""Compatibility types and entry point for governed tool execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from penhin.result import Result
from penhin.tools.catalog import ToolCatalog
from penhin.tools.registry import DEFAULT_TOOL_CATALOG
from penhin.tools.types import ToolInput, ToolOutcome

from .approval import ApprovalFlow, PermissionPolicy, runtime_permission_setup
from .validation import validate_tool_input


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False
    approval_required: bool = False
    effects: list[dict[str, str]] = field(default_factory=list)


def check_tool_access(tool_name: str, tool_input: ToolInput, policy: PermissionPolicy, approval: ApprovalFlow, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ToolRun | None:
    if tool_name in policy.deny:
        return ToolRun(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))
    spec = catalog.get(tool_name)
    if spec is None:
        return ToolRun(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))
    if tool_name not in policy.allow:
        return ToolRun(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))
    if approval.is_rejected(tool_name, tool_input, catalog):
        return ToolRun(Result.failure(f"Approval rejected for tool: {tool_name}", code="tool_approval_rejected"))
    if spec.approval.requires_approval and not approval.is_approved(tool_name, tool_input, catalog):
        return ToolRun(Result.failure(f"Approval required for tool: {tool_name}", code="tool_approval_required"), approval_required=True)
    return None


def run_tool(tool_name: str, tool_input: ToolInput, policy: PermissionPolicy, approval: ApprovalFlow = None, context: Any = None, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ToolRun:
    """Compatibility delegate; ToolInvocation is the only execution seam."""
    from .invocation import ToolInvocation
    return ToolInvocation().invoke(tool_name, tool_input, policy, approval, context, catalog)


def execute_tool(tool_name: str, tool_input: ToolInput, context: Any = None, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ToolRun:
    """Legacy handler-level helper; model calls must use ``run_tool``/Invocation."""
    spec = catalog.get(tool_name)
    if spec is None or spec.handler is None:
        return ToolRun(Result.failure(f"Unknown tool handler: {tool_name}", code="unknown_tool_handler"))
    invalid = validate_tool_input(tool_name, tool_input, catalog)
    if invalid:
        return ToolRun(invalid)
    try:
        outcome = spec.handler(**tool_input)
    except TypeError as error:
        return ToolRun(Result.failure(f"Invalid input for {tool_name}: {error}", code="invalid_tool_input"))
    if not isinstance(outcome, ToolOutcome):
        return ToolRun(Result.failure(f"Tool {tool_name} returned no ToolOutcome", code="invalid_tool_outcome"))
    from .invocation import ToolInvocation
    return ToolInvocation().apply_outcome(tool_name, outcome, context)
