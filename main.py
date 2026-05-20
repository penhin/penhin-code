#!/usr/bin/env python3

import sys

from agent import agent_loop, print_last_text, run_once
from runtime import init_runtime
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY
from transcript import transcripts


def main() -> None:
    init_runtime()

    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        run_once(" ".join(sys.argv[2:]))
        return

    messages = []
    approval = ApprovalFlow.require_confirmation(PARENT_AGENT_POLICY.allow)

    while True:
        try:
            user_input = input("penhin >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input in {"", "q", "quit", "exit"}:
            break

        messages.append({"role": "user", "content": user_input})
        agent_loop(messages, approval)
        transcripts.save(messages)
        print_last_text(messages)


if __name__ == "__main__":
    main()
