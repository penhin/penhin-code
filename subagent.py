import logging
from typing import Any

from permissions import permission_setup
from prompt import (
    build_verification_system,
    build_subagent_final_system,
    build_subagent_system,
    ensure_project_instructions_message,
)
from result import Result
from tools.registry import CHILD_TOOLS, TOOL_SPECS
from tools.types import ToolSchema, tool_schema
from runtime import get_runtime, log_usage
from message_flow import execute_tool_blocks, extract_text
from tool_runtime import CHILD_AGENT_APPROVAL_FLOW, CHILD_AGENT_POLICY


logger = logging.getLogger("penhin.subagent")


def tools_for_policy(tool_names: set[str]) -> list[ToolSchema]:
    return [
        tool_schema(spec)
        for name, spec in TOOL_SPECS.items()
        if name in tool_names
    ]


def verification_agent_config() -> dict[str, Any]:
    policy, approval = permission_setup("verification")
    return {
        "system": build_verification_system,
        "final_system": build_verification_system,
        "tools": tools_for_policy(policy.allow),
        "policy": policy,
        "approval": approval,
    }


AGENT_TYPES = {
    "general": {
        "system": build_subagent_system,
        "final_system": build_subagent_final_system,
        "tools": CHILD_TOOLS,
        "policy": CHILD_AGENT_POLICY,
        "approval": CHILD_AGENT_APPROVAL_FLOW,
    },
    "verification": verification_agent_config,
}


def agent_config(agent_type: str) -> dict[str, Any] | None:
    config = AGENT_TYPES.get(agent_type)
    if callable(config):
        return config()
    return config


def request_final_summary(runtime, sub_messages: list[dict[str, Any]], config: dict[str, Any], agent_type: str) -> str:
    ensure_project_instructions_message(sub_messages)
    response = runtime.call_with_retry(
        system=config["final_system"](),
        messages=sub_messages,
        max_tokens=runtime.sub_max_tokens,
    )
    log_usage(f"subagent-{agent_type}-final", response)
    return extract_text(response.content, default="(no summary)")


def run_subagent(task: str, agent_type: str = "general") -> Result:
    config = agent_config(agent_type)
    if config is None:
        return Result.failure(
            f"Unknown agent_type: {agent_type}",
            code="unknown_agent_type",
            data={"agent_type": agent_type, "available": sorted(AGENT_TYPES)},
        )

    runtime = get_runtime()
    
    sub_messages = [{"role": "user", "content": task}]
    ensure_project_instructions_message(sub_messages)
    
    for _ in range(0, runtime.sub_max_turns):
        response = runtime.call_with_retry(
            system=config["system"](),
            messages=sub_messages,
            tools=config["tools"],
            max_tokens=runtime.sub_max_tokens
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        
        log_usage(f"subagent-{agent_type}", response)
        if response.stop_reason != "tool_use":
            return Result.success(extract_text(response.content, default="(no summary)"))

        tool_results, _ = execute_tool_blocks(
            response.content,
            config["policy"],
            config["approval"],
        )

        if not tool_results:
            return Result.success(extract_text(response.content, default="(no summary)"))

        sub_messages.append({"role": "user", "content": tool_results})
    
    try:
        return Result.success(request_final_summary(runtime, sub_messages, config, agent_type))
    except Exception as error:
        return Result.failure(
            f"Subagent failed to summarize after max turns: {error}",
            code="summary_failed",
        )
    

        
        
    
