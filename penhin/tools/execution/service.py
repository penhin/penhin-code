from __future__ import annotations

import itertools
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from penhin.result import Result
from penhin.tools.registry import TOOL_SPECS
from penhin.tools.types import ToolInput
from .approval import ApprovalFlow, PermissionPolicy, default_approval_flow, runtime_permission_setup
from .observability import log_tool_blocked, log_tool_done, log_tool_start, short_hash
from .validation import unknown_tool_input_fields, validate_tool_input

if TYPE_CHECKING:
    from penhin.agent.context import RunContext

logger = logging.getLogger("penhin.tool_runtime")
logger.addHandler(logging.NullHandler())

_TOOL_CALL_COUNTER = itertools.count(1)


def next_tool_call_id() -> str:
    return f"tool-{next(_TOOL_CALL_COUNTER)}"


@dataclass
class ToolRun:
    result: Result
    manual_compact: bool = False
    approval_required: bool = False


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
        if tool_name == "snip":
            return execute_snip_tool(tool_input, context)
        return ToolRun(Result.failure(f"Unknown tool handler: {tool_name}", code="unknown_tool_handler"))

    try:
        if tool_name in {"enter_plan", "exit_plan"}:
            return ToolRun(spec.handler(context=context, **tool_input))
        return ToolRun(spec.handler(**tool_input))
    except TypeError as error:
        return ToolRun(Result.failure(f"Invalid input for {tool_name}: {error}", code="invalid_tool_input"))
    except Exception as error:
        return ToolRun(Result.failure(f"Tool {tool_name} failed: {error}", code="tool_error"))


def execute_snip_tool(tool_input: ToolInput, context: RunContext | None) -> ToolRun:
    if context is None:
        return ToolRun(Result.failure("No active session to snip.", code="missing_context"))

    selectors_input = tool_input.get("selectors")
    if isinstance(selectors_input, str):
        selector_texts = selectors_input.split()
    elif isinstance(selectors_input, list):
        selector_texts = [str(selector) for selector in selectors_input]
    else:
        return ToolRun(Result.failure("Invalid input: selectors must be an array", code="invalid_tool_input"))

    try:
        from penhin.agent.context import parse_snip_selectors
        selectors = parse_snip_selectors(selector_texts)
    except ValueError:
        return ToolRun(
            Result.failure(
                "Invalid snip selector. Use turn numbers or ranges like 2 or 2-4.",
                code="invalid_tool_input",
            )
        )

    snipped = context.force_snip_turns(selectors)
    return ToolRun(Result.success(f"Marked {snipped} messages as snipped.", snipped=snipped))


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
        from penhin.evaluation.observer import emit
        emit(
            "tool_call_completed", tool_name=tool_name, input_digest=short_hash(tool_input),
            status="approval_required" if access_run.approval_required else "blocked",
            duration_ms=duration_ms, code=access_run.result.meta.get("code"),
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
    from penhin.evaluation.observer import emit
    emit(
        "tool_call_completed", tool_name=tool_name, input_digest=short_hash(tool_input),
        status="ok" if tool_run.result.ok else "error", duration_ms=duration_ms,
        code=tool_run.result.meta.get("code"), unknown_input_fields=unknown_fields,
    )

    return tool_run
