import json
import os
from typing import Any

from compact import auto_compact_messages, micro_compact_text, should_auto_compact
from runtime import get_runtime, print_usage
from skills import load_skill
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY, approval_key, run_tool
from tools import PARENT_TOOLS
from transcript import transcripts


SYSTEM = (
    f"You are Penhin Code, a tiny coding agent running in {os.getcwd()}. "
    "Use task_start/task_show/task_complete/task_block/task_clear/task_list/task_switch to track the high-level task state. "
    "Use background_start/background_list/background_show for focused tasks that can run while the main conversation continues. "
    "Use todo_set/todo_show/todo_done/todo_clear to plan and track multi-step tasks before making changes. "
    "Use task to delegate focused subtasks that benefit from fresh context. "
    "Use list/search/read/edit/write/workspace for file operations. "
    "Use load_skill when a listed skill is relevant and you need its full instructions. "
    "Use compact when context is getting long, tool results are noisy, or before switching tasks. "
    "Use bash only for running commands, tests, or inspecting runtime behavior. "
    "Prefer structured tools over ad hoc shell commands for file operations. "
    "Tool results are JSON with ok/message/data/error/meta fields; prefer data for structured facts and error for failures. "
    "Ignore .venv, .git, __pycache__, skills, and internal state files."
    "\n\nAvailable skills:\n"
    f"{load_skill.get_descriptions()}"
)


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
    print(f"[approval] tool: {tool_name}")
    print(f"[approval] key: {approval_key(tool_name, tool_input)}")
    print(format_tool_input(tool_input))

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
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=runtime.max_tokens,
        )

        messages.append({"role": "assistant", "content": response.content})

        print_usage("main", response)

        if response.stop_reason != "tool_use":
            return

        tool_results = []
        manual_compact = False
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            print(f"$ AI use {tool_name}...")

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
            print(output_text)

            tool_results.append(
                {
                    "type": "tool_result",
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
                print(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            print(block.text)
