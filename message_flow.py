from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from tool_runtime import ApprovalFlow, PermissionPolicy, ToolRun, run_tool

if TYPE_CHECKING:
    from context import RunContext


ToolResults = list[dict[str, Any]]
ApprovalResolver = Callable[[str, dict[str, Any], PermissionPolicy, ApprovalFlow], ToolRun]

TOOL_RESULT_CACHE_MIN_CHARS = 2048
CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}

logger = logging.getLogger("penhin.message_flow")


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


def execute_tool_blocks(
    content: Any,
    policy: PermissionPolicy,
    approval: ApprovalFlow,
    approval_resolver: ApprovalResolver | None = None,
    context: RunContext | None = None,
) -> tuple[ToolResults, bool]:
    tool_results = []
    manual_compact = False

    if not isinstance(content, list):
        return tool_results, manual_compact

    for block in content:
        if block_get(block, "type") != "tool_use":
            continue

        tool_name = block_get(block, "name")
        tool_input = block_get(block, "input", {})
        tool_use_id = block_get(block, "id")
        logger.info(f"$ AI use {tool_name}...")

        tool_run = run_tool(tool_name, tool_input, policy, approval, context=context)
        if tool_run.approval_required and approval_resolver is not None:
            tool_run = approval_resolver(tool_name, tool_input, policy, approval)

        if tool_run.manual_compact:
            manual_compact = True

        tool_results.append(tool_result_block(tool_name, tool_use_id, tool_run))

    add_tool_result_cache_control(tool_results)
    return tool_results, manual_compact
