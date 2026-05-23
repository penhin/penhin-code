#!/usr/bin/env python3

import sys
import logging
import time

from runtime import init_runtime
from transcript import transcripts
from agent import agent_loop, print_last_text, run_once
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY


logger = logging.getLogger("penhin.main")


def has_flag(*names: str) -> bool:
    return any(name in sys.argv[1:] for name in names)


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] in {"--sessions", "-s"}:
        print_session_list()
        return

    if len(sys.argv) >= 3 and sys.argv[1] in {"--inspect-session", "-i"}:
        print_session_inspect(sys.argv[2])
        return

    init_runtime()

    if len(sys.argv) >= 3 and sys.argv[1] in {"--once", "-o"}:
        run_once(" ".join(sys.argv[2:]))
        return

    new_session = has_flag("--new", "-n")
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


def print_session_list() -> None:
    sessions = transcripts.list()
    if not sessions:
        print("No sessions found.")
        return

    print("id | updated | messages | first user")
    for session in sessions:
        updated = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(session.updated_at),
        )
        first_user = session.first_user or "-"
        print(f"{session.id} | {updated} | {session.message_count} | {first_user}")


def print_session_inspect(session_ref: str) -> None:
    try:
        session = transcripts.inspect(session_ref)
    except Exception as error:
        print(f"Session inspect failed: {error}")
        sys.exit(1)

    role_counts = ", ".join(
        f"{role}={count}"
        for role, count in sorted(session.role_counts.items())
    )
    print(f"id: {session.id}")
    print(f"path: {session.path}")
    print(f"messages: {session.message_count}")
    print(f"roles: {role_counts or '-'}")
    print(f"first_user: {session.first_user or '-'}")
    print(f"last_assistant: {session.last_assistant or '-'}")


if __name__ == "__main__":
    main()
