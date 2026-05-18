from dataclasses import dataclass

from result import Result
from tools import TOOL_HANDLERS


@dataclass
class PermissionPolicy:
    allow: set[str]
    deny: set[str]


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False


@dataclass
class ToolEvent:
    name: str
    input: dict
    phase: str


def run_tool(tool_name: str, tool_input: dict, policy: PermissionPolicy) -> ToolRun:
    if tool_name in policy.deny:
        return ToolRun(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))

    if tool_name not in policy.allow:
        return ToolRun(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))

    handler = TOOL_HANDLERS.get(tool_name, None)
    if handler is None:
        if tool_name == "compact":
            return ToolRun(
                result=Result.success("Compacting conversation history now"),
                manual_compact=True,
            )
        return ToolRun(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))

    try:
        return ToolRun(handler(**tool_input))
    except TypeError as error:
        return ToolRun(Result.failure(f"Invalid input for {tool_name}: {error}", code="invalid_tool_input"))
    except Exception as error:
        return ToolRun(Result.failure(f"Tool {tool_name} failed: {error}", code="tool_error"))
