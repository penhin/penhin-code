from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from result import Result


class ToolCategory(Enum):
    readonly = 0
    state = 1
    write = 2
    shell = 3
    agent = 4


ToolInput = dict[str, Any]
ToolSchema = dict[str, Any]
ApprovalKey = Callable[[ToolInput], str]


@dataclass
class ToolApproval:
    requires_approval: bool = False
    key: ApprovalKey | None = None

    def approval_key(self, tool_name: str, tool_input: ToolInput) -> str:
        if self.key is None:
            return tool_name
        return f"{tool_name}:{self.key(tool_input)}"


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: ToolSchema
    category: ToolCategory
    handler: Callable[..., Result] | None
    available_to_child: bool = True
    available_to_parent: bool = True
    approval: ToolApproval = field(default_factory=ToolApproval)


def tool_schema(spec: ToolSpec) -> ToolSchema:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }
