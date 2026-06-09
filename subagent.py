import logging
from typing import Any

from prompt import build_subagent_final_system, build_subagent_system
from result import Result
from tools import CHILD_TOOLS
from runtime import get_runtime, log_usage
from tool_runtime import CHILD_AGENT_APPROVAL_FLOW, CHILD_AGENT_POLICY, run_tool


logger = logging.getLogger("penhin.subagent")


def extract_summary(content: Any) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            parts.append(block.text)
    return ("\n".join(parts)) or "(no summary)"


def request_final_summary(runtime, sub_messages: list[dict[str, Any]]) -> str:
    response = runtime.call_with_retry(
        system=build_subagent_final_system(),
        messages=sub_messages,
        max_tokens=runtime.sub_max_tokens,
    )
    log_usage("subagent-final", response)
    return extract_summary(response.content)


def run_subagent(task: str) -> Result:
    runtime = get_runtime()
    
    sub_messages = [{"role": "user", "content": task}]
    
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
            return Result.success(extract_summary(response.content))

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            logger.info(f"$ AI use {tool_name}...")

            output = run_tool(
                tool_name,
                block.input,
                CHILD_AGENT_POLICY,
                CHILD_AGENT_APPROVAL_FLOW,
            ).result

            output_text = output.to_json()

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_use_id": block.id,
                    "content": output_text,
                }
            )

        if not tool_results:
            return Result.success(extract_summary(response.content))

        sub_messages.append({"role": "user", "content": tool_results})
    
    try:
        return Result.success(request_final_summary(runtime, sub_messages))
    except Exception as error:
        return Result.failure(
            f"Subagent failed to summarize after max turns: {error}",
            code="summary_failed",
        )
    

        
        
    
