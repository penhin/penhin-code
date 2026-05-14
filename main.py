#!/usr/bin/env python3

import os
import sys

from result import Result
from skills import load_skill
from transcript import transcripts
from tools import TOOL_HANDLERS, PARENT_TOOLS
from runtime import init_runtime, get_runtime, print_usage
from compact import auto_compact_messages, micro_compact_text, should_auto_compact


SYSTEM = (
    f"You are Penhin Code, a tiny coding agent running in {os.getcwd()}. "
    "Use task_status to track the high-level task state. "
    "Use todo to plan and track multi-step tasks before making changes. "
    "Use task to delegate focused subtasks that benefit from fresh context. "
    "Use list/search/read/edit/write/workspace for file operations. "
    "Use load_skill when a listed skill is relevant and you need its full instructions. "
    "Use compact when context is getting long, tool results are noisy, or before switching tasks. "
    "Use bash only for running commands, tests, or inspecting runtime behavior. "
    "Prefer structured tools over ad hoc shell commands for file operations. "
    "Ignore .venv, .git, __pycache__, skills, and internal state files."
    "\n\nAvailable skills:\n"
    f"{load_skill.get_descriptions()}"
)

def agent_loop(messages: list[dict]) -> None:
    runtime = get_runtime()

    while True:
        micro_compact_text(messages)
        if should_auto_compact(messages):
            messages[:] = auto_compact_messages(messages)

        response = runtime.client.messages.create(
            model=runtime.model,
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

            handler = TOOL_HANDLERS.get(tool_name, None)            
            if handler is None:
                if tool_name == "compact":
                    manual_compact = True
                    output = Result(stdout="Compacting conversation history now")
                else:
                    output = Result(1, stderr=f"Unknown tool: {tool_name}")
            else:
                try:
                    output = handler(**block.input)
                except TypeError as e:
                    output = Result(1, stderr=f"Invalid input for {tool_name}: {e}")
                except Exception as e:
                    output = Result(1, stderr=f"Tool {tool_name} failed: {e}")

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
    agent_loop(messages)
    transcripts.save(messages)
    print_last_text(messages)


def print_last_text(messages: list[dict]) -> None:
    content = messages[-1]["content"]
    if not isinstance(content, list):
        return

    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                print(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            print(block.text)


def main() -> None:
    init_runtime()

    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        run_once(" ".join(sys.argv[2:]))
        return

    messages = []

    while True:
        try:
            user_input = input("penhin >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input in {"", "q", "quit", "exit"}:
            break

        messages.append({"role": "user", "content": user_input})
        agent_loop(messages)
        transcripts.save(messages)
        print_last_text(messages)


if __name__ == "__main__":
    main()
