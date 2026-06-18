from __future__ import annotations

import json
import time
import logging
import hashlib
import itertools

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from result import Result

if TYPE_CHECKING:
    from context import RunContext
from tools.registry import TOOL_SPECS
from tools.types import ToolCategory, ToolInput


logger = logging.getLogger("penhin.tool_runtime")
logger.addHandler(logging.NullHandler())

_TOOL_CALL_COUNTER = itertools.count(1)

SAFE_INPUT_FIELDS = {"path", "name", "id", "index", "action", "limit", "line_numbers"}
HASHED_INPUT_FIELDS = {"command", "content", "task", "description", "note", "old", "new", "items", "blocked_by"}

TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def next_tool_call_id() -> str:
    return f"tool-{next(_TOOL_CALL_COUNTER)}"


@dataclass
class PermissionPolicy:
    allow: set[str]
    deny: set[str] = field(default_factory=set)


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

    def copy(self) -> ApprovalFlow:
        return ApprovalFlow(
            approved=set(self.approved),
            required=set(self.required),
            rejected=set(self.rejected),
        )

    def approve(self, tool_name: str, tool_input: ToolInput) -> None:
        self.approved.add(approval_key(tool_name, tool_input))

    def reject(self, tool_name: str, tool_input: ToolInput) -> None:
        self.rejected.add(approval_key(tool_name, tool_input))

    def is_approved(self, tool_name: str, tool_input: ToolInput) -> bool:
        return tool_name in self.approved or approval_key(tool_name, tool_input) in self.approved

    def is_rejected(self, tool_name: str, tool_input: ToolInput) -> bool:
        return tool_name in self.rejected or approval_key(tool_name, tool_input) in self.rejected


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False
    approval_required: bool = False


def tool_names_for(scope: str) -> set[str]:
    if scope == "parent":
        return {name for name, spec in TOOL_SPECS.items() if spec.available_to_parent}
    if scope == "child":
        return {name for name, spec in TOOL_SPECS.items() if spec.available_to_child}
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
        if name in TOOL_SPECS and TOOL_SPECS[name].approval.requires_approval
    }


PARENT_AGENT_ALLOW = tool_names_for("parent")
CHILD_AGENT_ALLOW = tool_names_for("child")


PARENT_AGENT_POLICY = PermissionPolicy(
    allow=PARENT_AGENT_ALLOW,
    deny=set(),
)
PARENT_AGENT_APPROVAL_FLOW = ApprovalFlow.preapproved(PARENT_AGENT_ALLOW)


CHILD_AGENT_POLICY = PermissionPolicy(
    allow=CHILD_AGENT_ALLOW,
)
CHILD_AGENT_APPROVAL_FLOW = ApprovalFlow.preapproved(CHILD_AGENT_ALLOW)


def policy_for_runtime_mode(mode: str) -> PermissionPolicy:
    if mode == "auto-review":
        allow = tool_names_by_category({ToolCategory.readonly, ToolCategory.state})
        if "compact" in TOOL_SPECS:
            allow.add("compact")
        return PermissionPolicy(
            allow=allow,
            deny={"write", "edit", "bash", "task", "background_start"},
        )

    return PARENT_AGENT_POLICY


def approval_for_runtime_mode(mode: str, policy: PermissionPolicy) -> ApprovalFlow:
    if mode in {"auto-review", "full-access"}:
        return ApprovalFlow.preapproved(policy.allow)

    return ApprovalFlow.require_confirmation(policy.allow)


def runtime_permission_setup(mode: str) -> tuple[PermissionPolicy, ApprovalFlow]:
    policy = policy_for_runtime_mode(mode)
    return policy, approval_for_runtime_mode(mode, policy)


def default_approval_flow(policy: PermissionPolicy) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow)


def approval_key(tool_name: str, tool_input: ToolInput) -> str:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return tool_name
    return spec.approval.approval_key(tool_name, tool_input)


def short_hash(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def value_size(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def input_summary(tool_input: ToolInput) -> str:
    if not tool_input:
        return "<none>"

    parts = []
    for key in sorted(tool_input):
        value = tool_input[key]
        if key in SAFE_INPUT_FIELDS:
            parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        elif key in HASHED_INPUT_FIELDS:
            parts.append(f"{key}_sha={json.dumps(short_hash(value))}")
            parts.append(f"{key}_chars={value_size(value)}")
        else:
            parts.append(f"{key}=<hidden:{type(value).__name__}>")
    return " ".join(parts)


def log_tool_start(tool_id: str, tool_name: str, tool_input: ToolInput) -> None:
    logger.info(f"[tool] start call_id={tool_id} name={tool_name} input={input_summary(tool_input)}")


def format_result_summary(result: Result) -> str:
    summary = result.summary()
    return " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in summary.items()
    )


def log_tool_done(tool_id: str, tool_name: str, tool_run: ToolRun, duration_ms: float) -> None:
    result = tool_run.result
    result_summary = format_result_summary(result)
    flags = (
        f"manual_compact={json.dumps(tool_run.manual_compact)} "
        f"approval_required={json.dumps(tool_run.approval_required)}"
    )
    if result.ok:
        logger.info(
            f"[tool] done call_id={tool_id} name={tool_name} "
            f"status=ok duration_ms={duration_ms:.2f} {flags} {result_summary}"
        )
        return

    code = result.meta.get("code", "unknown")
    logger.error(
        f"[tool] done call_id={tool_id} name={tool_name} "
        f"status=error duration_ms={duration_ms:.2f} code={code} {flags} {result_summary}"
    )


def log_tool_blocked(
    tool_id: str,
    tool_name: str,
    tool_input: ToolInput,
    result: Result,
    duration_ms: float,
    status: str,
) -> None:
    code = result.meta.get("code", "unknown")
    logger.warning(
        f"[tool] blocked call_id={tool_id} name={tool_name} "
        f"status={status} duration_ms={duration_ms:.2f} "
        f"input={input_summary(tool_input)} code={code} {format_result_summary(result)}"
    )


def check_tool_access(
    tool_name: str,
    tool_input: ToolInput,
    policy: PermissionPolicy,
    approval: ApprovalFlow,
) -> ToolRun | None:
    if tool_name in policy.deny:
        return ToolRun(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))

    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return ToolRun(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))

    if tool_name not in policy.allow:
        return ToolRun(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))

    if approval.is_rejected(tool_name, tool_input):
        return ToolRun(Result.failure(f"Approval rejected for tool: {tool_name}", code="tool_approval_rejected"))

    if spec.approval.requires_approval and not approval.is_approved(tool_name, tool_input):
        return ToolRun(
            Result.failure(f"Approval required for tool: {tool_name}", code="tool_approval_required"),
            approval_required=True,
        )

    return None


def unknown_tool_input_fields(tool_name: str, tool_input: ToolInput) -> list[str]:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return []
    
    properties = spec.input_schema.get("properties", {})
    allowed = set(properties)
    return sorted(set(tool_input) - allowed)


def validate_tool_input(tool_name: str, tool_input: ToolInput) -> Result | None:
    spec = TOOL_SPECS.get(tool_name)
    properties = spec.input_schema.get("properties", {})
    required = spec.input_schema.get("required", [])
    
    missing = [
        name for name in required
        if name not in tool_input
    ]
    
    if missing:
        return Result.failure(
            f"Missing required input: {', '.join(missing)}",
            code="invalid_tool_input",
            missing=missing
        )
        
    for name, value in tool_input.items():
        field_schema = properties.get(name)
        if field_schema is None:
            continue

        expected = field_schema.get("type")
        checker = TYPE_CHECKS.get(expected)
        
        if checker is None:
            continue

        if not checker(value):
            return Result.failure(
                f"Invalid input type: {name} expected {expected}",
                code="invalid_tool_input",
                field=name,
                expected=expected,
                actual=type(value).__name__,
            )

        enum_values = field_schema.get("enum")
        if enum_values is not None and value not in enum_values:
            return Result.failure(
                f"Invalid input value: {name} must be one of {', '.join(map(str, enum_values))}",
                code="invalid_tool_input",
                field=name,
                expected="enum",
                allowed=enum_values,
                actual=value,
            )

    return None


def execute_tool(
    tool_name: str,
    tool_input: ToolInput,
    context: RunContext | None = None,
) -> ToolRun:
    spec = TOOL_SPECS[tool_name]

    invalid = validate_tool_input(tool_name, tool_input)
    if invalid:
        return ToolRun(invalid)

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
    tool_input: ToolInput,
    policy: PermissionPolicy,
    approval: ApprovalFlow = None,
    context: RunContext | None = None,
) -> ToolRun:
    approval = approval or default_approval_flow(policy)

    call_id = next_tool_call_id()
    start = time.perf_counter()
    access_run = check_tool_access(tool_name, tool_input, policy, approval)

    if access_run is not None:
        duration_ms = (time.perf_counter() - start) * 1000
        log_tool_blocked(
            call_id,
            tool_name,
            tool_input,
            access_run.result,
            duration_ms,
            "approval_required" if access_run.approval_required else "blocked",
        )
        return access_run

    unknown_fields = unknown_tool_input_fields(tool_name, tool_input)
    if unknown_fields:
        logger.warning(
            f"[tool] unknown_input call_id={call_id} "
            f"name={tool_name} fields={json.dumps(unknown_fields, ensure_ascii=False)}"
        )

    log_tool_start(call_id, tool_name, tool_input)
    tool_run = execute_tool(tool_name, tool_input, context)

    duration_ms = (time.perf_counter() - start) * 1000
    log_tool_done(call_id, tool_name, tool_run, duration_ms)

    return tool_run
