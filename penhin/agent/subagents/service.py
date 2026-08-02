import logging
import time
from typing import Any

from penhin.agent.state import (
    AgentDeps,
    AgentPhase,
    AgentState,
    TerminalReason,
    finish,
    is_terminal,
    step_agent,
)
from penhin.runtime.retry import CircuitBreakerOpen
from penhin.agent.context import RunContext
from penhin.agent.messages import build_tool_execution_context, execute_tool_blocks, extract_text
from penhin.agent.projection import messages_for_api
from penhin.agent.prompts import (
    build_exploration_final_system,
    build_exploration_system,
    build_plan_agent_final_system,
    build_plan_agent_system,
    build_subagent_final_system,
    build_subagent_system,
    build_verification_system,
    ensure_project_instructions_message,
)
from penhin.result import Result
from penhin.runtime import runtime_manager
from penhin.runtime.manager import log_usage
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.tools.registry import TOOL_SPECS
from penhin.tools.types import ToolSchema, tool_schema


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


def build_subagent_deps(
    runtime,
    config: dict[str, Any],
    execution_context,
    agent_type: str,
    max_tokens: int,
    last_response: dict[str, Any],
    budget_exhausted: dict[str, bool],
) -> AgentDeps:
    turn_counter = {"value": 0}

    def call_subagent_llm(context: RunContext):
        start = time.perf_counter()
        response = runtime.call_with_retry(
            system=config["system"](),
            messages=messages_for_api(context.messages),
            tools=config["tools"],
            max_tokens=max_tokens,
        )
        duration_ms = (time.perf_counter() - start) * 1000
        turn_counter["value"] += 1
        log_subagent_turn(agent_type, turn_counter["value"], response, duration_ms)
        last_response["value"] = response
        return response

    def record_subagent_response(context: RunContext, response) -> None:
        context.add_assistant_message(response.content)
        log_usage(f"subagent-{agent_type}", response)

    def execute_subagent_tools(context: RunContext, response):
        return execute_tool_blocks(response.content, execution_context)

    def record_subagent_tool_results(
        context: RunContext,
        tool_results: list[dict[str, Any]],
        _manual_compact: bool,
    ) -> None:
        context.add_tool_results(tool_results)
        budget_exhausted["value"] = (
            execution_context.max_tool_calls is not None
            and execution_context.tool_calls_used >= execution_context.max_tool_calls
        )

    def handle_subagent_circuit_open(_context: RunContext, error: CircuitBreakerOpen) -> None:
        logger.warning(f"[circuit] subagent stopped: {error}")

    return AgentDeps(
        compact_context=lambda _context: None,
        call_llm=call_subagent_llm,
        record_llm_response=record_subagent_response,
        should_continue_with_tools=lambda response: response.stop_reason == "tool_use",
        execute_tool_uses=execute_subagent_tools,
        record_tool_results=record_subagent_tool_results,
        handle_circuit_open=handle_subagent_circuit_open,
    )


def run_subagent_state_machine(
    context: RunContext,
    deps: AgentDeps,
    max_turns: int,
    budget_exhausted: dict[str, bool],
) -> AgentState:
    state = AgentState()
    while not is_terminal(state) and not budget_exhausted["value"]:
        if state.phase == AgentPhase.COMPACT_CONTEXT and state.turn >= max_turns:
            break
        state = step_agent(context, state, deps)

    if is_terminal(state):
        return state

    if budget_exhausted["value"]:
        return finish(state, TerminalReason.TOOL_BUDGET_EXHAUSTED)

    return finish(state, TerminalReason.MAX_TURNS)


def run_subagent(task: str, agent_type: str = "general") -> Result:
    config = agent_config(agent_type)
    if config is None:
        return Result.failure(
            f"Unknown agent_type: {agent_type}",
            code="unknown_agent_type",
            data={"agent_type": agent_type, "available": sorted(AGENT_TYPES)},
        )

    runtime = runtime_manager.current()
    sub_messages = build_subagent_initial_messages(task)
    max_turns = config["max_turns"] or runtime.sub_max_turns
    max_tokens = subagent_max_tokens(runtime, config)
    execution_context = build_tool_execution_context(
        config["policy"],
        config["approval"],
        max_tool_calls=config["max_tool_calls"],
    )
    context = RunContext(
        messages=sub_messages,
        policy=config["policy"],
        approval=config["approval"],
    )
    last_response: dict[str, Any] = {"value": None}
    budget_exhausted = {"value": False}
    tool_trace: list[dict[str, Any]] = []
    deps = build_subagent_deps(
        runtime,
        config,
        execution_context,
        agent_type,
        max_tokens,
        last_response,
        budget_exhausted,
    )

    def success(message: str) -> Result:
        return Result.success(message, tool_results=tool_trace)

    state = run_subagent_state_machine(context, deps, max_turns, budget_exhausted)
    sub_messages = context.messages
    response = last_response["value"]
    tool_trace = [
        block
        for message in context.messages
        for block in (message.get("content", []) if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]

    if state.terminal_reason == TerminalReason.CIRCUIT_OPEN:
        return Result.failure(
            API_UNAVAILABLE_MESSAGE,
            code="circuit_open",
            data={"agent_type": agent_type},
        )

    if state.terminal_reason == TerminalReason.TOOL_BUDGET_EXHAUSTED:
        result = summarize_incomplete_response(
            runtime,
            sub_messages,
            config,
            agent_type,
            "tool_budget_exhausted",
        )
        return success(result.message) if result.ok else result

    if state.terminal_reason == TerminalReason.MAX_TURNS:
        try:
            return success(request_final_summary(runtime, sub_messages, config, agent_type))
        except Exception as error:
            return Result.failure(
                f"Subagent failed to summarize after max turns: {error}",
                code="summary_failed",
            )

    if state.terminal_reason == TerminalReason.NO_TOOL_RESULTS:
        return success(
            extract_text(getattr(response, "content", []), default="(no summary)")
        )

    if response is not None and getattr(response, "stop_reason", "") == "max_tokens":
        result = summarize_incomplete_response(
            runtime,
            sub_messages,
            config,
            agent_type,
            "max_tokens",
        )
        return success(result.message) if result.ok else result

    if response is not None:
        text = response_text(response)
        if text:
            return success(text)

        logger.warning(
            f"[subagent] empty_text_summary agent_type={agent_type} "
            f"turn={state.turn} stop_reason={getattr(response, 'stop_reason', '')} "
            f"{usage_summary(response)}"
        )
        result = summarize_incomplete_response(
            runtime,
            sub_messages,
            config,
            agent_type,
            "empty_text",
        )
        return success(result.message) if result.ok else result

    return Result.failure(
        "Subagent stopped without a model response.",
        code="missing_response",
    )
