import logging
import time
from typing import Any

from circuit_breaker import CircuitBreakerOpen
from message_flow import build_tool_execution_context, execute_tool_blocks, extract_text
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


def usage_summary(response) -> str:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return f"input={input_tokens} output={output_tokens} total={input_tokens + output_tokens}"


def log_subagent_turn(agent_type: str, turn: int | str, response, duration_ms: float) -> None:
    content = getattr(response, "content", [])
    block_count = len(content) if isinstance(content, list) else 0
    logger.info(
        f"[subagent] turn={turn} agent_type={agent_type} "
        f"duration_ms={duration_ms:.2f} stop_reason={getattr(response, 'stop_reason', '')} "
        f"blocks={block_count} {usage_summary(response)}"
    )


def response_text(response) -> str:
    return extract_text(getattr(response, "content", []), default="").strip()


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


def _agent_type_config(
    system,
    final_system,
    allow: set[str],
    max_turns: int | None = None,
    max_tokens: int | None = None,
    final_max_tokens: int | None = None,
    max_tool_calls: int | None = None,
) -> dict[str, Any]:
    policy = PermissionPolicy(allow=_available_tool_names(allow))
    return {
        "system": system,
        "final_system": final_system,
        "tools": tools_for_policy(policy.allow),
        "policy": policy,
        "approval": ApprovalFlow.preapproved(policy.allow),
        "max_turns": max_turns,
        "max_tokens": max_tokens,
        "final_max_tokens": final_max_tokens,
        "max_tool_calls": max_tool_calls,
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
        max_turns=6,
        max_tokens=800,
        final_max_tokens=2000,
        max_tool_calls=8,
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


def subagent_max_tokens(runtime, config: dict[str, Any]) -> int:
    return config["max_tokens"] or runtime.sub_max_tokens


def subagent_final_max_tokens(runtime, config: dict[str, Any]) -> int:
    return config["final_max_tokens"] or subagent_max_tokens(runtime, config)


def request_final_summary(runtime, sub_messages: list[dict[str, Any]], config: dict[str, Any], agent_type: str) -> str:
    ensure_project_instructions_message(sub_messages)
    start = time.perf_counter()
    try:
        response = runtime.call_with_retry(
            system=config["final_system"](),
            messages=messages_for_api(sub_messages),
            max_tokens=subagent_final_max_tokens(runtime, config),
        )
    except CircuitBreakerOpen as error:
        raise RuntimeError(f"{API_UNAVAILABLE_MESSAGE} ({error})") from error
    duration_ms = (time.perf_counter() - start) * 1000
    log_subagent_turn(agent_type, "final", response, duration_ms)
    log_usage(f"subagent-{agent_type}-final", response)
    return extract_text(response.content, default="(no summary)")


def summarize_incomplete_response(runtime, sub_messages: list[dict[str, Any]], config: dict[str, Any], agent_type: str, reason: str) -> Result:
    logger.warning(f"[subagent] incomplete_summary agent_type={agent_type} reason={reason}")
    try:
        return Result.success(request_final_summary(runtime, sub_messages, config, agent_type))
    except Exception as error:
        return Result.failure(
            f"Subagent failed to summarize incomplete final response: {error}",
            code="summary_failed",
        )


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
    max_turns = config["max_turns"] or runtime.sub_max_turns
    max_tokens = subagent_max_tokens(runtime, config)
    execution_context = build_tool_execution_context(
        config["policy"],
        config["approval"],
        max_tool_calls=config["max_tool_calls"],
    )
    
    for turn in range(1, max_turns + 1):
        start = time.perf_counter()
        try:
            response = runtime.call_with_retry(
                system=config["system"](),
                messages=messages_for_api(sub_messages),
                tools=config["tools"],
                max_tokens=max_tokens,
            )
        except CircuitBreakerOpen as error:
            logger.warning(f"[circuit] subagent stopped: {error}")
            return Result.failure(
                API_UNAVAILABLE_MESSAGE,
                code="circuit_open",
                data={"agent_type": agent_type},
            )
        duration_ms = (time.perf_counter() - start) * 1000
        log_subagent_turn(agent_type, turn, response, duration_ms)
        sub_messages.append({"role": "assistant", "content": response.content})
        
        log_usage(f"subagent-{agent_type}", response)
        if response.stop_reason != "tool_use":
            if response.stop_reason == "max_tokens":
                return summarize_incomplete_response(
                    runtime,
                    sub_messages,
                    config,
                    agent_type,
                    "max_tokens",
                )

            text = response_text(response)
            if text:
                return Result.success(text)

            logger.warning(
                f"[subagent] empty_text_summary agent_type={agent_type} "
                f"turn={turn} stop_reason={response.stop_reason} {usage_summary(response)}"
            )
            return summarize_incomplete_response(
                runtime,
                sub_messages,
                config,
                agent_type,
                "empty_text",
            )

        tool_results, _ = execute_tool_blocks(
            response.content,
            execution_context,
        )

        if not tool_results:
            return Result.success(extract_text(response.content, default="(no summary)"))

        sub_messages.append({"role": "user", "content": tool_results})
        if (
            execution_context.max_tool_calls is not None
            and execution_context.tool_calls_used >= execution_context.max_tool_calls
        ):
            return summarize_incomplete_response(
                runtime,
                sub_messages,
                config,
                agent_type,
                "tool_budget_exhausted",
            )
    
    try:
        return Result.success(request_final_summary(runtime, sub_messages, config, agent_type))
    except Exception as error:
        return Result.failure(
            f"Subagent failed to summarize after max turns: {error}",
            code="summary_failed",
        )
