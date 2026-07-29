import json
import logging
from typing import Any

import ui

from agent_state import AgentDeps, AgentState, is_terminal, step_agent
from approval_rules import suggest_bash_prefix
from circuit_breaker import CircuitBreakerOpen
from context import RunContext
from message_flow import ToolResults, build_tool_execution_context, execute_tool_blocks
from message_projection import messages_for_api
from prompt import build_main_system, ensure_project_instructions_message
from runtime import get_runtime, log_usage
from tool_runtime import ApprovalFlow, PermissionPolicy, approval_key, run_tool
from tools.registry import PARENT_TOOLS
from transcript import transcripts


API_UNAVAILABLE_MESSAGE = "API is temporarily unavailable because the circuit breaker is open. Please try again later."

logger = logging.getLogger("penhin.agent")


def format_tool_input(tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return "{}"
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


def run_with_one_time_approval(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
):
    one_time_approval = approval.copy()
    one_time_approval.approve(tool_name, tool_input)
    return run_tool(
        tool_name,
        tool_input,
        policy,
        one_time_approval,
    )


def run_with_one_time_rejection(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
):
    one_time_rejection = approval.copy()
    one_time_rejection.reject(tool_name, tool_input)
    return run_tool(
        tool_name,
        tool_input,
        policy,
        one_time_rejection,
    )


def resolve_approval(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
):
    logger.info(f"[approval] tool: {tool_name}")
    logger.info(f"[approval] key: {approval_key(tool_name, tool_input)}")
    logger.info(format_tool_input(tool_input))
    suggested_prefix = None
    if tool_name == "bash":
        command = str(tool_input.get("command", ""))
        suggested_prefix = suggest_bash_prefix(command)
        print("[approval] bash")
        print(command)
        print()
        print("1. allow once")
        print("2. allow exact command this session")
        if suggested_prefix:
            print(f"3. allow command prefix this session: {suggested_prefix}")
        else:
            print("3. allow command prefix this session: unavailable")
        print("4. reject")

    try:
        reply = input("[approval] choose 1-4 [4] ").strip().lower()
    except EOFError:
        logger.info("[approval] no input available; rejecting")
        reply = ""
    if reply in {"1", "y"}:
        return run_with_one_time_approval(tool_name, tool_input, policy, approval)

    if reply in {"2", "ys"}:
        approval.approve(tool_name, tool_input)
        return run_tool(
            tool_name,
            tool_input,
            policy,
            approval,
        )

    if reply in {"3", "yp"} and suggested_prefix:
        approval.approve_rule(tool_name, suggested_prefix)
        return run_tool(
            tool_name,
            tool_input,
            policy,
            approval,
        )

    return run_with_one_time_rejection(tool_name, tool_input, policy, approval)


def compact_context_for_llm(context: RunContext) -> None:
    force_compact_hint = context.consume_force_compact_hint()
    if force_compact_hint is not None:
        context.force_auto_compact(hint=force_compact_hint)
        context.collapse_keep_recent = None
        return

    context.micro_compact()
    context.auto_compact_if_needed()


def call_llm(context: RunContext, runtime):
    ensure_project_instructions_message(context.messages)
    streamed = False
    stream = None

    def on_stream_text(text: str) -> None:
        nonlocal streamed, stream
        if not streamed:
            stream = ui.start_assistant_message()
        streamed = True
        stream.write(text)

    try:
        return runtime.call_with_retry(
            system=build_main_system(),
            messages=messages_for_api(
                context.messages,
                collapse_keep_recent=context.collapse_keep_recent,
            ),
            tools=PARENT_TOOLS,
            max_tokens=runtime.max_tokens,
            stream_callback=on_stream_text,
        )
    finally:
        if streamed:
            ui.finish_stream(stream)

def record_llm_response(context: RunContext, response) -> None:
    context.add_assistant_message(response.content)
    log_usage("main", response)


def should_continue_with_tools(response) -> bool:
    return response.stop_reason == "tool_use"


def execute_tool_uses(context: RunContext, response) -> tuple[ToolResults, bool]:
    return execute_tool_blocks(
        response.content,
        build_tool_execution_context(
            context.policy,
            context.approval,
            approval_resolver=resolve_approval,
            context=context,
        ),
    )


def record_tool_results(context: RunContext, tool_results: ToolResults, manual_compact: bool) -> None:
    context.add_tool_results(tool_results)

    if manual_compact:
        context.request_force_compact()


def handle_circuit_open(context: RunContext, error: CircuitBreakerOpen) -> None:
    logger.warning(f"[circuit] {API_UNAVAILABLE_MESSAGE} ({error})")
    context.add_assistant_message([
        {"type": "text", "text": API_UNAVAILABLE_MESSAGE}
    ])


def build_agent_deps(runtime) -> AgentDeps:
    return AgentDeps(
        compact_context=compact_context_for_llm,
        call_llm=lambda context: call_llm(context, runtime),
        record_llm_response=record_llm_response,
        should_continue_with_tools=should_continue_with_tools,
        execute_tool_uses=execute_tool_uses,
        record_tool_results=record_tool_results,
        handle_circuit_open=handle_circuit_open,
    )


def run_agent_state_machine(
    context: RunContext,
    deps: AgentDeps,
    initial_state: AgentState | None = None,
) -> AgentState:
    state = initial_state or AgentState()
    while not is_terminal(state):
        state = step_agent(context, state, deps)
    return state


def agent_loop(context: RunContext) -> AgentState:
    runtime = get_runtime()
    return run_agent_state_machine(context, build_agent_deps(runtime))


def run_once(query: str) -> None:
    from config import get_permission_mode
    from tool_runtime import runtime_permission_setup

    policy, approval = runtime_permission_setup(get_permission_mode())
    context = RunContext(
        messages=[{"role": "user", "content": query}],
        policy=policy,
        approval=approval,
    )
    agent_loop(context)
    transcripts.save(context.messages)
