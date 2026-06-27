import json
import logging
from typing import Any

import ui

from circuit_breaker import CircuitBreakerOpen
from context import RunContext
from message_flow import ToolResults, execute_tool_blocks
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

    reply = input("[approval] y=once, ys=session, n=reject [y/N] ").strip().lower()
    if reply == "y":
        return run_with_one_time_approval(tool_name, tool_input, policy, approval)

    if reply == "ys":
        approval.approve(tool_name, tool_input)
        return run_tool(
            tool_name,
            tool_input,
            policy,
            approval,
        )

    return run_with_one_time_rejection(tool_name, tool_input, policy, approval)


def compact_context_for_llm(context: RunContext) -> None:
    context.micro_compact()
    context.auto_compact_if_needed()


def call_llm(context: RunContext, runtime):
    ensure_project_instructions_message(context.messages)
    streamed = False

    def on_stream_text(text: str) -> None:
        nonlocal streamed
        streamed = True
        ui.print_stream_delta(text)

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
            ui.finish_stream()

def record_llm_response(context: RunContext, response) -> None:
    context.add_assistant_message(response.content)
    log_usage("main", response)


def should_continue_with_tools(response) -> bool:
    return response.stop_reason == "tool_use"


def execute_tool_uses(context: RunContext, response) -> tuple[ToolResults, bool]:
    return execute_tool_blocks(
        response.content,
        context.policy,
        context.approval,
        approval_resolver=resolve_approval,
        context=context,
    )


def record_tool_results(context: RunContext, tool_results: ToolResults, manual_compact: bool) -> None:
    context.add_tool_results(tool_results)

    if manual_compact:
        context.force_auto_compact()


def agent_loop(context: RunContext) -> None:
    runtime = get_runtime()

    while True:
        compact_context_for_llm(context)

        try:
            response = call_llm(context, runtime)
        except CircuitBreakerOpen as error:
            logger.warning(f"[circuit] {API_UNAVAILABLE_MESSAGE} ({error})")
            context.add_assistant_message([
                {"type": "text", "text": API_UNAVAILABLE_MESSAGE}
            ])
            return

        record_llm_response(context, response)

        if not should_continue_with_tools(response):
            return

        tool_results, manual_compact = execute_tool_uses(context, response)

        if not tool_results:
            return

        record_tool_results(context, tool_results, manual_compact)


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
