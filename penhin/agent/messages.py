"""Model-message projection; governed execution belongs to ToolInvocation."""
from typing import Any

from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.tools.execution.invocation import ToolCall, ToolExecutionContext, ToolInvocation, collect_tool_calls
from penhin.tools.registry import DEFAULT_TOOL_CATALOG

ToolResults = list[dict[str, Any]]
TOOL_RESULT_CACHE_MIN_CHARS = 2048
CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}

def block_get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)

def build_tool_execution_context(policy: PermissionPolicy, approval: ApprovalFlow, approval_resolver=None, context=None, max_tool_calls=None, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ToolExecutionContext:
    return ToolExecutionContext(policy, approval, approval_resolver, context, max_tool_calls, catalog=catalog)

def extract_text(content: Any, default: str = "") -> str:
    if not isinstance(content, list): return default
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text") or default

def cacheable_tool_result(block: dict[str, Any]) -> bool:
    return isinstance(block.get("content"), str) and len(block["content"]) >= TOOL_RESULT_CACHE_MIN_CHARS

def add_tool_result_cache_control(tool_results: ToolResults) -> None:
    for block in reversed(tool_results):
        if cacheable_tool_result(block):
            block["cache_control"] = dict(CACHE_CONTROL_EPHEMERAL)
            return

def execute_tool_blocks(content: Any, execution_context: ToolExecutionContext) -> tuple[ToolResults, bool]:
    results, manual_compact = ToolInvocation().execute_blocks(content, execution_context)
    add_tool_result_cache_control(results)
    return results, manual_compact
