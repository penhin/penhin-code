#!/usr/bin/env python3

import sys
import logging

from runtime import init_runtime
from transcript import transcripts
from agent import agent_loop, print_last_text, run_once
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY


logger = logging.getLogger("penhin.main")


def main() -> None:
    init_runtime()

    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        run_once(" ".join(sys.argv[2:]))
        return

    new_session = "--new" in sys.argv[1:]
    messages = load_initial_messages(resume=not new_session)
    approval = ApprovalFlow.require_confirmation(PARENT_AGENT_POLICY.allow)

    while True:
        try:
            user_input = input("penhin >> ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("")
            break

        if user_input in {"", "q", "quit", "exit"}:
            break

        messages.append({"role": "user", "content": user_input})
        agent_loop(messages, approval)
        transcripts.save(messages)
        print_last_text(messages)


def load_initial_messages(resume: bool) -> list[dict]:
    if not resume:
        logger.info("[session] new reason=flag")
        return []

    history_file = transcripts.latest()
    if not history_file:
        logger.info("[session] new reason=no_history")
        return []

    try:
        messages = transcripts.read(history_file)
        logger.info(f"[session] resumed {history_file}")
        return messages
    except Exception as error:
        logger.warning(f"[session] resume failed: {error}")
        logger.info("[session] new reason=resume_failed")
        return []


if __name__ == "__main__":
    main()
