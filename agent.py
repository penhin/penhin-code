import json
import logging
from typing import Any

from compact import auto_compact_messages, micro_compact_text, should_auto_compact
from prompt import build_main_system
from runtime import get_runtime, log_usage
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY, approval_key, run_tool
from tools import PARENT_TOOLS
from transcript import transcripts


logger = logging.getLogger("penhin.agent")


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


def agent_loop(messages: list[dict[str, Any]], approval: ApprovalFlow = None) -> None:
    runtime = get_runtime()

    approval = approval or ApprovalFlow.require_confirmation(PARENT_AGENT_POLICY.allow)

    while True:
        micro_compact_text(messages)
        if should_auto_compact(messages):
            messages[:] = auto_compact_messages(messages)

        response = runtime.call_with_retry(
            system=build_main_system(),
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=runtime.max_tokens,
        )

        messages.append({"role": "assistant", "content": response.content})

        log_usage("main", response)

        if response.stop_reason != "tool_use":
            return

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
                approval,
            )

            output = tool_run.result

            if tool_run.approval_required:
                tool_run = resolve_approval(tool_name, block.input, approval)
                output = tool_run.result

            if tool_run.manual_compact:
                manual_compact = True

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
            return

        messages.append({"role": "user", "content": tool_results})

        if manual_compact:
            messages[:] = auto_compact_messages(messages)


def run_once(query: str) -> None:
    messages = [{"role": "user", "content": query}]
    approval = ApprovalFlow.require_confirmation(PARENT_AGENT_POLICY.allow)
    agent_loop(messages, approval)
    transcripts.save(messages)
    print_last_text(messages)


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
