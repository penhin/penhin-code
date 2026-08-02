from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from concurrent.futures import ThreadPoolExecutor

from penhin.result import Result
from penhin.tools.execution import ApprovalFlow, PermissionPolicy, ToolRun, run_tool
from penhin.tools.registry import TOOL_SPECS

if TYPE_CHECKING:
    from penhin.agent.context import RunContext


ToolResults = list[dict[str, Any]]
ApprovalResolver = Callable[[str, dict[str, Any], PermissionPolicy, ApprovalFlow], ToolRun]

TOOL_RESULT_CACHE_MIN_CHARS = 2048
CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}

logger = logging.getLogger("penhin.message_flow")
DELEGATION_TOOLS = {"task", "verify"}


@dataclass(frozen=True)
class ToolCall:
    index: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str


@dataclass
class ToolExecutionContext:
    policy: PermissionPolicy
    approval: ApprovalFlow
    approval_resolver: ApprovalResolver | None = None
    run_context: RunContext | None = None
    max_tool_calls: int | None = None
    tool_calls_used: int = 0


def build_tool_execution_context(
    policy: PermissionPolicy,
    approval: ApprovalFlow,
    approval_resolver: ApprovalResolver | None = None,
    context: RunContext | None = None,
    max_tool_calls: int | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        policy=policy,
        approval=approval,
        approval_resolver=approval_resolver,
        run_context=context,
        max_tool_calls=max_tool_calls,
    )


def block_get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def extract_text(content: Any, default: str = "") -> str:
    if not isinstance(content, list):
        return default

    parts = []
    for block in content:
        if block_get(block, "type") == "text":
            parts.append(block_get(block, "text", ""))
    return "\n".join(parts) or default


def tool_result_block(tool_name: str, tool_use_id: str, tool_run: ToolRun) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "content": tool_run.result.to_json(),
    }


def cacheable_tool_result(block: dict[str, Any]) -> bool:
    content = block.get("content")
    return isinstance(content, str) and len(content) >= TOOL_RESULT_CACHE_MIN_CHARS


def add_tool_result_cache_control(tool_results: ToolResults) -> None:
    for block in reversed(tool_results):
        if cacheable_tool_result(block):
            block["cache_control"] = dict(CACHE_CONTROL_EPHEMERAL)
            return


def collect_tool_calls(content: Any) -> list[ToolCall]:
    if not isinstance(content, list):
        return []

    calls = []
    for index, block in enumerate(content):
        if block_get(block, "type") != "tool_use":
            continue

        calls.append(
            ToolCall(
                index=index,
                tool_name=str(block_get(block, "name", "")),
                tool_input=block_get(block, "input", {}) or {},
                tool_use_id=str(block_get(block, "id", "")),
            )
        )
    return calls


def tool_call_is_parallel_safe(call: ToolCall) -> bool:
    spec = TOOL_SPECS.get(call.tool_name)
    if spec is None:
        return False
    if spec.approval.requires_approval:
        return False
    return spec.parallel_safe


def tool_budget_block(call: ToolCall, execution_context: ToolExecutionContext) -> Result | None:
    if execution_context.max_tool_calls is None:
        return None

    if execution_context.tool_calls_used >= execution_context.max_tool_calls:
        return Result.failure(
            (
                f"Tool budget exhausted before {call.tool_name}. "
                "Stop calling tools and return the best answer from the evidence already collected."
            ),
            code="tool_budget_exhausted",
            blocked_tool=call.tool_name,
            tool_calls_used=execution_context.tool_calls_used,
            max_tool_calls=execution_context.max_tool_calls,
        )

    execution_context.tool_calls_used += 1
    return None


def run_tool_call(call: ToolCall, execution_context: ToolExecutionContext) -> tuple[dict[str, Any], bool]:
    logger.info(f"$ AI use {call.tool_name}...")

    blocked_result = tool_budget_block(call, execution_context) or (
        execution_context.run_context.post_delegation_tool_block(call.tool_name)
        if execution_context.run_context is not None
        else None
    )
    if blocked_result is not None:
        logger.warning(
            f"[delegation_guard] blocked tool={call.tool_name} "
            f"code={blocked_result.meta.get('code')} "
            f"source={blocked_result.meta.get('source_tool')} "
            f"read_budget_remaining={blocked_result.meta.get('read_budget_remaining')}"
        )
        tool_run = ToolRun(blocked_result)
    else:
        tool_run = run_tool(
            call.tool_name,
            call.tool_input,
            execution_context.policy,
            execution_context.approval,
            context=execution_context.run_context,
        )

    if tool_run.approval_required and execution_context.approval_resolver is not None:
        tool_run = execution_context.approval_resolver(
            call.tool_name,
            call.tool_input,
            execution_context.policy,
            execution_context.approval,
        )

    if execution_context.run_context is not None and call.tool_name in DELEGATION_TOOLS and tool_run.result.ok:
        execution_context.run_context.activate_post_delegation_guard(call.tool_name)

    return tool_result_block(call.tool_name, call.tool_use_id, tool_run), tool_run.manual_compact


def run_parallel_safe_calls(
    calls: list[ToolCall],
    execution_context: ToolExecutionContext,
) -> tuple[ToolResults, bool]:
    if not calls:
        return [], False

    from penhin.evaluation.observer import emit
    emit("parallel_tool_batch_started", tool_names=[call.tool_name for call in calls], call_count=len(calls))

    blocked_results: dict[int, tuple[dict[str, Any], bool]] = {}
    runnable_calls = []
    for call in calls:
        logger.info(f"$ AI use {call.tool_name}...")
        blocked_result = tool_budget_block(call, execution_context) or (
            execution_context.run_context.post_delegation_tool_block(call.tool_name)
            if execution_context.run_context is not None
            else None
        )
        if blocked_result is None:
            runnable_calls.append(call)
            continue

        logger.warning(
            f"[delegation_guard] blocked tool={call.tool_name} "
            f"code={blocked_result.meta.get('code')} "
            f"source={blocked_result.meta.get('source_tool')} "
            f"read_budget_remaining={blocked_result.meta.get('read_budget_remaining')}"
        )
        blocked_results[call.index] = (
            tool_result_block(call.tool_name, call.tool_use_id, ToolRun(blocked_result)),
            False,
        )

    run_results: dict[int, tuple[dict[str, Any], bool]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                run_tool,
                call.tool_name,
                call.tool_input,
                execution_context.policy,
                execution_context.approval,
                context=execution_context.run_context,
            ): call
            for call in runnable_calls
        }

        for future, call in futures.items():
            tool_run = future.result()
            if tool_run.approval_required and execution_context.approval_resolver is not None:
                tool_run = execution_context.approval_resolver(
                    call.tool_name,
                    call.tool_input,
                    execution_context.policy,
                    execution_context.approval,
                )
            run_results[call.index] = (
                tool_result_block(call.tool_name, call.tool_use_id, tool_run),
                tool_run.manual_compact,
            )

    all_results = {**blocked_results, **run_results}
    ordered = [all_results[call.index] for call in sorted(calls, key=lambda c: c.index)]
    return [result for result, _manual_compact in ordered], any(
        manual_compact for _result, manual_compact in ordered
    )


def append_parallel_batch(
    batch: list[ToolCall],
    execution_context: ToolExecutionContext,
    tool_results: ToolResults,
    manual_compact: bool,
) -> bool:
    if not batch:
        return manual_compact

    batch_results, batch_manual_compact = run_parallel_safe_calls(batch, execution_context)
    tool_results.extend(batch_results)
    return manual_compact or batch_manual_compact


def execute_tool_blocks(
    content: Any,
    execution_context: ToolExecutionContext,
) -> tuple[ToolResults, bool]:
    tool_results = []
    manual_compact = False
    parallel_batch: list[ToolCall] = []

    calls = collect_tool_calls(content)
    if not calls:
        return tool_results, manual_compact

    for call in calls:
        if tool_call_is_parallel_safe(call):
            parallel_batch.append(call)
            continue

        manual_compact = append_parallel_batch(
            parallel_batch,
            execution_context,
            tool_results,
            manual_compact,
        )
        parallel_batch = []
        result_block, call_manual_compact = run_tool_call(call, execution_context)
        tool_results.append(result_block)
        manual_compact = manual_compact or call_manual_compact

    manual_compact = append_parallel_batch(
        parallel_batch,
        execution_context,
        tool_results,
        manual_compact,
    )

    add_tool_result_cache_control(tool_results)
    return tool_results, manual_compact
