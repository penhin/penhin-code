import logging
from typing import Any

from circuit_breaker import CircuitBreakerOpen
from message_flow import execute_tool_blocks, extract_text
from message_projection import messages_for_api
from prompt import (
    build_exploration_final_system,
    build_exploration_system,
    build_plan_agent_final_system,
    build_plan_agent_system,
    build_subagent_final_system,
    build_subagent_system,
    build_verification_system,
    ensure_project_instructions_message,
)
from result import Result
from runtime import get_runtime, log_usage
from tool_runtime import ApprovalFlow, PermissionPolicy
from tools.registry import TOOL_SPECS
from tools.types import ToolSchema, tool_schema


API_UNAVAILABLE_MESSAGE = "API is temporarily unavailable because the circuit breaker is open. Please try again later."

logger = logging.getLogger("penhin.subagent")


def tools_for_policy(tool_names: set[str]) -> list[ToolSchema]:
    return [
        tool_schema(spec)
        for name, spec in TOOL_SPECS.items()
        if name in tool_names
    ]


def _available_tool_names(tool_names: set[str]) -> set[str]:
    return {name for name in tool_names if name in TOOL_SPECS}


def _readonly_tool_allowlist() -> set[str]:
    return _available_tool_names({
        "glob",
        "list",
        "read",
        "search",
        "workspace",
    })


def _verification_tool_allowlist() -> set[str]:
    return _available_tool_names({
        "bash",
        "compact",
        "glob",
        "list",
        "read",
        "search",
        "task_show",
        "todo_show",
        "workspace",
    })


def _child_tool_allowlist() -> set[str]:
    return {
        name
        for name, spec in TOOL_SPECS.items()
        if spec.available_to_child
    }


def _agent_type_config(system, final_system, allow: set[str]) -> dict[str, Any]:
    policy = PermissionPolicy(allow=_available_tool_names(allow))
    return {
        "system": system,
        "final_system": final_system,
        "tools": tools_for_policy(policy.allow),
        "policy": policy,
        "approval": ApprovalFlow.preapproved(policy.allow),
    }


def _general_config() -> dict[str, Any]:
    return _agent_type_config(
        build_subagent_system,
        build_subagent_final_system,
        _child_tool_allowlist(),
    )


def _explore_config() -> dict[str, Any]:
    return _agent_type_config(
        build_exploration_system,
        build_exploration_final_system,
        _readonly_tool_allowlist(),
    )


def _verify_config() -> dict[str, Any]:
    return _agent_type_config(
        build_verification_system,
        build_verification_system,
        _verification_tool_allowlist(),
    )


def _plan_config() -> dict[str, Any]:
    return _agent_type_config(
        build_plan_agent_system,
        build_plan_agent_final_system,
        _readonly_tool_allowlist(),
    )


AGENT_TYPES = {
    "general": _general_config,
    "explore": _explore_config,
    "verification": _verify_config,
    "plan": _plan_config,
}


def agent_config(agent_type: str) -> dict[str, Any] | None:
    config_factory = AGENT_TYPES.get(agent_type)
    if config_factory is None:
        return None
    return config_factory()


def build_subagent_initial_messages(task: str) -> list[dict[str, Any]]:
    sub_messages = [{"role": "user", "content": task}]
    ensure_project_instructions_message(sub_messages)
    return sub_messages


def request_final_summary(runtime, sub_messages: list[dict[str, Any]], config: dict[str, Any], agent_type: str) -> str:
    ensure_project_instructions_message(sub_messages)
    try:
        response = runtime.call_with_retry(
            system=config["final_system"](),
            messages=messages_for_api(sub_messages),
            max_tokens=runtime.sub_max_tokens,
        )
    except CircuitBreakerOpen as error:
        raise RuntimeError(f"{API_UNAVAILABLE_MESSAGE} ({error})") from error
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
    sub_messages = build_subagent_initial_messages(task)
    
    for _ in range(0, runtime.sub_max_turns):
        try:
            response = runtime.call_with_retry(
                system=config["system"](),
                messages=messages_for_api(sub_messages),
                tools=config["tools"],
                max_tokens=runtime.sub_max_tokens
            )
        except CircuitBreakerOpen as error:
            logger.warning(f"[circuit] subagent stopped: {error}")
            return Result.failure(
                API_UNAVAILABLE_MESSAGE,
                code="circuit_open",
                data={"agent_type": agent_type},
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
