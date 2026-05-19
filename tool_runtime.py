from dataclasses import dataclass, field

from result import Result
from tools import TOOL_SPECS, ToolCategory


@dataclass
class PermissionPolicy:
    allow: set[str]
    deny: set[str]
    approved: set[str] = field(default_factory=set)


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False


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


def preapproved_tools(tool_names: set[str]) -> set[str]:
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
    approved=preapproved_tools(ASK_ALLOW),
)


EDIT_POLICY = PermissionPolicy(
    allow=EDIT_ALLOW,
    deny={"bash"},
    approved=preapproved_tools(EDIT_ALLOW),
)


PARENT_AGENT_POLICY = PermissionPolicy(
    allow=PARENT_AGENT_ALLOW,
    deny=set(),
    approved=preapproved_tools(PARENT_AGENT_ALLOW),
)


CHILD_AGENT_POLICY = PermissionPolicy(
    allow=CHILD_AGENT_ALLOW,
    deny=set(),
    approved=preapproved_tools(CHILD_AGENT_ALLOW),
)


def run_tool(tool_name: str, tool_input: dict, policy: PermissionPolicy) -> ToolRun:
    if tool_name in policy.deny:
        return ToolRun(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))

    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return ToolRun(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))

    if tool_name not in policy.allow:
        return ToolRun(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))

    if spec.requires_approval and tool_name not in policy.approved:
        return ToolRun(Result.failure(f"Approval required for tool: {tool_name}", code="tool_approval_required"))

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
