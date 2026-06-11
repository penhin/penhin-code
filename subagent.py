import logging
from typing import Any

from prompt import (
    build_subagent_final_system,
    build_subagent_system,
    ensure_project_instructions_message,
)
from result import Result
from tools.registry import CHILD_TOOLS
from runtime import get_runtime, log_usage
from message_flow import execute_tool_blocks, extract_text
from tool_runtime import CHILD_AGENT_APPROVAL_FLOW, CHILD_AGENT_POLICY


logger = logging.getLogger("penhin.subagent")


def request_final_summary(runtime, sub_messages: list[dict[str, Any]]) -> str:
    ensure_project_instructions_message(sub_messages)
    response = runtime.call_with_retry(
        system=build_subagent_final_system(),
        messages=sub_messages,
        max_tokens=runtime.sub_max_tokens,
    )
    log_usage("subagent-final", response)
    return extract_text(response.content, default="(no summary)")


def run_subagent(task: str) -> Result:
    runtime = get_runtime()
    
    sub_messages = [{"role": "user", "content": task}]
    ensure_project_instructions_message(sub_messages)
    
    for _ in range(0, runtime.sub_max_turns):
        response = runtime.call_with_retry(
            system=build_subagent_system(),
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=runtime.sub_max_tokens
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        
        log_usage("subagent", response)
        if response.stop_reason != "tool_use":
            return Result.success(extract_text(response.content, default="(no summary)"))

        tool_results, _ = execute_tool_blocks(
            response.content,
            CHILD_AGENT_POLICY,
            CHILD_AGENT_APPROVAL_FLOW,
        )

        if not tool_results:
            return Result.success(extract_text(response.content, default="(no summary)"))

        sub_messages.append({"role": "user", "content": tool_results})
    
    try:
        return Result.success(request_final_summary(runtime, sub_messages))
    except Exception as error:
        return Result.failure(
            f"Subagent failed to summarize after max turns: {error}",
            code="summary_failed",
        )
    

        
        
    
