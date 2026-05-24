#!/usr/bin/env python3

import sys
import time
import logging
import argparse

from pathlib import Path
from runtime import init_runtime
from transcript import transcripts
from agent import agent_loop, print_last_text, run_once
from tool_runtime import ApprovalFlow, PARENT_AGENT_POLICY


logger = logging.getLogger("penhin.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    
    parser.add_argument("--once", "-o", nargs="+")
    parser.add_argument("--sessions", "-s", action="store_true")
    parser.add_argument("--new", "-n", action="store_true")
    parser.add_argument("--inspect-session", "-i")
    parser.add_argument("--resume", "-r")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    if args.sessions:
        print_session_list()
        return

    if args.inspect_session:
        print_session_inspect(args.inspect_session)
        return

    init_runtime()

    if args.once:
        run_once(" ".join(args.once))
        return
    
    if args.new:
        messages = load_initial_messages(resume=False)
    elif args.resume:
        messages = load_initial_messages(resume=True, session_ref=args.resume)
    else:
        messages = load_initial_messages(resume=True)

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


def load_initial_messages(resume: bool, session_ref: Path | str | None = None) -> list[dict]:
    if not resume:
        logger.info("[session] new reason=flag")
        return []

    try:
        if session_ref is None:
            history_file = transcripts.latest()
            if history_file is None:
                logger.info("[session] new reason=no_history")
                return []
        else:
            history_file = transcripts.resolve_session_ref(str(session_ref))

        messages = transcripts.read(history_file)
        logger.info(f"[session] resumed {history_file}")
        return messages
    except Exception as error:
        logger.warning(f"[session] resume failed: {error}")
        logger.info("[session] new reason=resume_failed")
        return []


def print_session_list() -> None:
    sessions = sorted(
        transcripts.list(),
        key=lambda session: session.updated_at,
        reverse=True
    )
    if not sessions:
        print("No sessions found.")
        return

    print("id | updated | msgs | request")
    for session in sessions:
        updated = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(session.updated_at),
        )
        first_user = session.first_user or "-"
        print(f"{session.id[:12]} | {updated} | {session.message_count} | {first_user}")


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
