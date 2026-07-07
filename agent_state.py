from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable

from circuit_breaker import CircuitBreakerOpen
from context import RunContext
from message_flow import ToolResults


class AgentPhase(str, Enum):
    COMPACT_CONTEXT = "compact_context"
    CALL_MODEL = "call_model"
    RECORD_RESPONSE = "record_response"
    EXECUTE_TOOLS = "execute_tools"
    RECORD_TOOL_RESULTS = "record_tool_results"
    FINISHED = "finished"
    FAILED = "failed"


class TerminalReason(str, Enum):
    END_TURN = "end_turn"
    CIRCUIT_OPEN = "circuit_open"
    NO_TOOL_RESULTS = "no_tool_results"
    MAX_TURNS = "max_turns"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    ERROR = "error"


@dataclass(frozen=True)
class AgentState:
    phase: AgentPhase = AgentPhase.COMPACT_CONTEXT
    turn: int = 0
    response: Any = None
    tool_results: ToolResults | None = None
    manual_compact: bool = False
    last_stop_reason: str | None = None
    terminal_reason: TerminalReason | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentDeps:
    compact_context: Callable[[RunContext], None]
    call_llm: Callable[[RunContext], Any]
    record_llm_response: Callable[[RunContext, Any], None]
    should_continue_with_tools: Callable[[Any], bool]
    execute_tool_uses: Callable[[RunContext, Any], tuple[ToolResults, bool]]
    record_tool_results: Callable[[RunContext, ToolResults, bool], None]
    handle_circuit_open: Callable[[RunContext, CircuitBreakerOpen], None]


def is_terminal(state: AgentState) -> bool:
    return state.phase in {AgentPhase.FINISHED, AgentPhase.FAILED}


def finish(
    state: AgentState,
    reason: TerminalReason,
    phase: AgentPhase = AgentPhase.FINISHED,
    error: str | None = None,
) -> AgentState:
    return replace(
        state,
        phase=phase,
        response=None,
        tool_results=None,
        manual_compact=False,
        terminal_reason=reason,
        error=error,
    )


def step_agent(context: RunContext, state: AgentState, deps: AgentDeps) -> AgentState:
    if state.phase == AgentPhase.COMPACT_CONTEXT:
        deps.compact_context(context)
        return replace(state, phase=AgentPhase.CALL_MODEL)

    if state.phase == AgentPhase.CALL_MODEL:
        try:
            response = deps.call_llm(context)
        except CircuitBreakerOpen as error:
            deps.handle_circuit_open(context, error)
            return finish(
                state,
                TerminalReason.CIRCUIT_OPEN,
                phase=AgentPhase.FAILED,
                error=str(error),
            )
        return replace(state, phase=AgentPhase.RECORD_RESPONSE, response=response)

    if state.phase == AgentPhase.RECORD_RESPONSE:
        response = state.response
        deps.record_llm_response(context, response)
        next_state = replace(
            state,
            turn=state.turn + 1,
            last_stop_reason=getattr(response, "stop_reason", None),
        )
        if not deps.should_continue_with_tools(response):
            return finish(next_state, TerminalReason.END_TURN)
        return replace(next_state, phase=AgentPhase.EXECUTE_TOOLS)

    if state.phase == AgentPhase.EXECUTE_TOOLS:
        tool_results, manual_compact = deps.execute_tool_uses(context, state.response)
        if not tool_results:
            return finish(state, TerminalReason.NO_TOOL_RESULTS)
        return replace(
            state,
            phase=AgentPhase.RECORD_TOOL_RESULTS,
            tool_results=tool_results,
            manual_compact=manual_compact,
        )

    if state.phase == AgentPhase.RECORD_TOOL_RESULTS:
        assert state.tool_results is not None
        deps.record_tool_results(context, state.tool_results, state.manual_compact)
        return replace(
            state,
            phase=AgentPhase.COMPACT_CONTEXT,
            response=None,
            tool_results=None,
            manual_compact=False,
        )

    return state
