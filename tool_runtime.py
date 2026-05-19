from __future__ import annotations

from dataclasses import dataclass, field

from result import Result
from tools import TOOL_SPECS, ToolCategory


@dataclass
class PermissionPolicy:
    allow: set[str]
    deny: set[str]


@dataclass
class ApprovalFlow:
    approved: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=set)
    rejected: set[str] = field(default_factory=set)

    @classmethod
    def preapproved(cls, tool_names: set[str]) -> ApprovalFlow:
        required = approval_required_tools(tool_names)
        return cls(approved=required, required=required)

    @classmethod
    def require_confirmation(cls, tool_names: set[str]) -> ApprovalFlow:
        return cls(required=approval_required_tools(tool_names))


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False
    approval_required: bool = False


@dataclass
class ToolAccess:
    result: Result | None = None
    approval_required: bool = False

    @property
    def allowed(self) -> bool:
        return self.result is None and not self.approval_required


def tool_names_for(scope: str) -> set[str]:
    if scope == "parent":
        return {name for name, spec in TOOL_SPECS.items() if spec.parent}
    if scope == "child":
        return {name for name, spec in TOOL_SPECS.items() if spec.child}
    raise ValueError(f"Unknown tool scope: {scope}")


def tool_names_by_category(categories: set[ToolCategory]) -> set[str]:
    return {
        name
        for name, spec in TOOL_SPECS.items()
        if spec.category in categories
    }


def approval_required_tools(tool_names: set[str]) -> set[str]:
    return {
        name for name in tool_names
        if name in TOOL_SPECS and TOOL_SPECS[name].requires_approval
    }


ASK_ALLOW = tool_names_by_category({ToolCategory.readonly})
EDIT_ALLOW = tool_names_by_category({ToolCategory.readonly, ToolCategory.write, ToolCategory.state})
PARENT_AGENT_ALLOW = tool_names_for("parent")
CHILD_AGENT_ALLOW = tool_names_for("child")


ASK_POLICY = PermissionPolicy(
    allow=ASK_ALLOW,
    deny={"write", "edit", "bash", "task"},
)
ASK_APPROVAL_FLOW = ApprovalFlow.preapproved(ASK_ALLOW)


EDIT_POLICY = PermissionPolicy(
    allow=EDIT_ALLOW,
    deny={"bash"},
)
EDIT_APPROVAL_FLOW = ApprovalFlow.preapproved(EDIT_ALLOW)


PARENT_AGENT_POLICY = PermissionPolicy(
    allow=PARENT_AGENT_ALLOW,
    deny=set(),
)
PARENT_AGENT_APPROVAL_FLOW = ApprovalFlow.preapproved(PARENT_AGENT_ALLOW)


CHILD_AGENT_POLICY = PermissionPolicy(
    allow=CHILD_AGENT_ALLOW,
    deny=set(),
)
CHILD_AGENT_APPROVAL_FLOW = ApprovalFlow.preapproved(CHILD_AGENT_ALLOW)


def default_approval_flow(policy: PermissionPolicy) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow)


def check_tool_access(tool_name: str, policy: PermissionPolicy, approval: ApprovalFlow) -> ToolAccess:
    if tool_name in policy.deny:
        return ToolAccess(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))

    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return ToolAccess(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))

    if tool_name not in policy.allow:
        return ToolAccess(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))

    if tool_name in approval.rejected:
        return ToolAccess(Result.failure(f"Approval rejected for tool: {tool_name}", code="tool_approval_rejected"))

    if spec.requires_approval and tool_name not in approval.approved:
        return ToolAccess(
            Result.failure(f"Approval required for tool: {tool_name}", code="tool_approval_required"),
            approval_required=True,
        )

    return ToolAccess()


def execute_tool(tool_name: str, tool_input: dict) -> ToolRun:
    spec = TOOL_SPECS[tool_name]

    if spec.handler is None:
        if tool_name == "compact":
            return ToolRun(
                result=Result.success("Compacting conversation history now"),
                manual_compact=True,
            )
        return ToolRun(Result.failure(f"Unknown tool handler: {tool_name}", code="unknown_tool_handler"))

    try:
        return ToolRun(spec.handler(**tool_input))
    except TypeError as error:
        return ToolRun(Result.failure(f"Invalid input for {tool_name}: {error}", code="invalid_tool_input"))
    except Exception as error:
        return ToolRun(Result.failure(f"Tool {tool_name} failed: {error}", code="tool_error"))


def run_tool(
    tool_name: str,
    tool_input: dict,
    policy: PermissionPolicy,
    approval: ApprovalFlow = None,
) -> ToolRun:
    approval = approval or default_approval_flow(policy)

    access = check_tool_access(tool_name, policy, approval)
    if access.approval_required:
        return ToolRun(
            result=access.result,
            approval_required=True,
        )
    if not access.allowed:
        return ToolRun(access.result)

    return execute_tool(tool_name, tool_input)
