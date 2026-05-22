from __future__ import annotations

import hashlib
import json
import time
import logging
import itertools

from dataclasses import dataclass, field

from result import Result
from tools import TOOL_SPECS, ToolCategory, ToolInput


logger = logging.getLogger("penhin.tool")
logger.addHandler(logging.NullHandler())

_TOOL_CALL_COUNTER = itertools.count(1)

SAFE_INPUT_FIELDS = {"path", "name", "id", "index", "action", "limit", "line_numbers"}
HASHED_INPUT_FIELDS = {"command", "content", "task", "description", "note", "old", "new", "items", "blocked_by"}


def next_tool_call_id() -> str:
    return f"tool-{next(_TOOL_CALL_COUNTER)}"


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


@dataclass
class ToolAccess:
    result: Result | None = None
    approval_required: bool = False

    @property
    def allowed(self) -> bool:
        return self.result is None and not self.approval_required


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


def approval_key(tool_name: str, tool_input: ToolInput) -> str:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return tool_name
    return spec.approval.approval_key(tool_name, tool_input)


def short_hash(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def input_summary(tool_input: ToolInput) -> str:
    if not tool_input:
        return "<none>"

    parts = []
    for key in sorted(tool_input):
        value = tool_input[key]
        if key in SAFE_INPUT_FIELDS:
            parts.append(f"{key}:{value}")
        elif key in HASHED_INPUT_FIELDS:
            parts.append(f"{key}:sha256:{short_hash(value)}")
        else:
            parts.append(f"{key}:<hidden>")
    return ",".join(parts)


def log_tool_start(tool_id: str, tool_name: str, tool_input: ToolInput) -> None:
    logger.info(f"[tool] start call_id={tool_id} name={tool_name} input={input_summary(tool_input)}")


def format_result_summary(result: Result) -> str:
    summary = result.summary()
    return " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in summary.items()
    )


def log_tool_done(tool_id: str, tool_name: str, result: Result, duration_ms: float) -> None:
    result_summary = format_result_summary(result)
    if result.ok:
        logger.info(
            f"[tool] done call_id={tool_id} name={tool_name} "
            f"status=ok duration_ms={duration_ms:.2f} {result_summary}"
        )
        return

    code = result.meta.get("code", "unknown")
    logger.error(
        f"[tool] done call_id={tool_id} name={tool_name} "
        f"status=error duration_ms={duration_ms:.2f} code={code} {result_summary}"
    )


def log_tool_blocked(tool_id: str, tool_name: str, tool_input: ToolInput, result: Result) -> None:
    code = result.meta.get("code", "unknown")
    logger.warning(
        f"[tool] blocked call_id={tool_id} name={tool_name} "
        f"input={input_summary(tool_input)} code={code} {format_result_summary(result)}"
    )


def check_tool_access(
    tool_name: str,
    tool_input: ToolInput,
    policy: PermissionPolicy,
    approval: ApprovalFlow,
) -> ToolAccess:
    if tool_name in policy.deny:
        return ToolAccess(Result.failure(f"Denied by policy: {tool_name}", code="tool_denied"))

    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return ToolAccess(Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool"))

    if tool_name not in policy.allow:
        return ToolAccess(Result.failure(f"Not allowed by policy: {tool_name}", code="tool_not_allowed"))

    if approval.is_rejected(tool_name, tool_input):
        return ToolAccess(Result.failure(f"Approval rejected for tool: {tool_name}", code="tool_approval_rejected"))

    if spec.approval.requires_approval and not approval.is_approved(tool_name, tool_input):
        return ToolAccess(
            Result.failure(f"Approval required for tool: {tool_name}", code="tool_approval_required"),
            approval_required=True,
        )

    return ToolAccess()


def execute_tool(tool_name: str, tool_input: ToolInput) -> ToolRun:
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
    tool_input: ToolInput,
    policy: PermissionPolicy,
    approval: ApprovalFlow = None,
) -> ToolRun:
    approval = approval or default_approval_flow(policy)

    access = check_tool_access(tool_name, tool_input, policy, approval)
    if access.result is not None:
        call_id = next_tool_call_id()
        log_tool_blocked(call_id, tool_name, tool_input, access.result)

    if access.approval_required:
        return ToolRun(
            result=access.result,
            approval_required=True,
        )
    if not access.allowed:
        return ToolRun(access.result)

    call_id = next_tool_call_id()
    log_tool_start(call_id, tool_name, tool_input)
    start = time.perf_counter()
    tool_run = execute_tool(tool_name, tool_input)

    result = tool_run.result

    end = time.perf_counter()
    duration = (end - start) * 1000
    log_tool_done(call_id, tool_name, result, duration)

    return tool_run
