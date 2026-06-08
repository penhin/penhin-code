import json
import logging
from typing import Any

from context import RunContext
from prompt import build_main_system
from runtime import get_runtime, log_usage
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY, approval_key, run_tool
from tools import PARENT_TOOLS
from transcript import transcripts


logger = logging.getLogger("penhin.agent")

ToolResults = list[dict[str, Any]]


def format_tool_input(tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return "{}"
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


def run_with_one_time_approval(tool_name: str, tool_input: dict[str, Any], approval: ApprovalFlow):
    one_time_approval = approval.copy()
    one_time_approval.approve(tool_name, tool_input)
    return run_tool(
        tool_name,
        tool_input,
        PARENT_AGENT_POLICY,
        one_time_approval,
    )


def run_with_one_time_rejection(tool_name: str, tool_input: dict[str, Any], approval: ApprovalFlow):
    one_time_rejection = approval.copy()
    one_time_rejection.reject(tool_name, tool_input)
    return run_tool(
        tool_name,
        tool_input,
        PARENT_AGENT_POLICY,
        one_time_rejection,
    )


def resolve_approval(tool_name: str, tool_input: dict[str, Any], approval: ApprovalFlow):
    logger.info(f"[approval] tool: {tool_name}")
    logger.info(f"[approval] key: {approval_key(tool_name, tool_input)}")
    logger.info(format_tool_input(tool_input))

    reply = input("[approval] y=once, ys=session, n=reject [y/N] ").strip().lower()
    if reply == "y":
        return run_with_one_time_approval(tool_name, tool_input, approval)

    if reply == "ys":
        approval.approve(tool_name, tool_input)
        return run_tool(
            tool_name,
            tool_input,
            PARENT_AGENT_POLICY,
            approval,
        )

    return run_with_one_time_rejection(tool_name, tool_input, approval)


def compact_context_for_llm(context: RunContext) -> None:
    context.micro_compact()
    context.auto_compact_if_needed()


def call_llm(context: RunContext, runtime):
    return runtime.call_with_retry(
        system=build_main_system(),
        messages=context.messages,
        tools=PARENT_TOOLS,
        max_tokens=runtime.max_tokens,
    )


def record_llm_response(context: RunContext, response) -> None:
    context.add_assistant_message(response.content)
    log_usage("main", response)


def should_continue_with_tools(response) -> bool:
    return response.stop_reason == "tool_use"


def execute_tool_uses(context: RunContext, response) -> tuple[ToolResults, bool]:
    tool_results = []
    manual_compact = False
    for block in response.content:
        if block.type != "tool_use":
            continue

        tool_name = block.name
        logger.info(f"$ AI use {tool_name}...")

        tool_run = run_tool(
            tool_name,
            block.input,
            PARENT_AGENT_POLICY,
            context.approval,
        )

        output = tool_run.result

        if tool_run.approval_required:
            tool_run = resolve_approval(tool_name, block.input, context.approval)
            output = tool_run.result

        if tool_run.manual_compact:
            manual_compact = True

        tool_results.append(
            {
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_use_id": block.id,
                "content": output.to_json(),
            }
        )

    return tool_results, manual_compact


def record_tool_results(context: RunContext, tool_results: ToolResults, manual_compact: bool) -> None:
    context.add_tool_results(tool_results)

    if manual_compact:
        context.force_auto_compact()


def agent_loop(context: RunContext) -> None:
    runtime = get_runtime()

    while True:
        compact_context_for_llm(context)

        response = call_llm(context, runtime)

        record_llm_response(context, response)

        if not should_continue_with_tools(response):
            return

        tool_results, manual_compact = execute_tool_uses(context, response)

        if not tool_results:
            return

        record_tool_results(context, tool_results, manual_compact)


def run_once(query: str) -> None:
    approval = ApprovalFlow.require_confirmation(PARENT_AGENT_POLICY.allow)
    context = RunContext(
        messages=[{"role": "user", "content": query}],
        approval=approval,
    )
    agent_loop(context)
    transcripts.save(context.messages)
    print_last_text(context.messages)


def print_last_text(messages: list[dict[str, Any]]) -> None:
    content = messages[-1]["content"]
    if not isinstance(content, list):
        return

    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                logger.info(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            logger.info(block.text)
