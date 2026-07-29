#!/usr/bin/env python3

import argparse
import logging
import os
import sys
import time

from agent import agent_loop, run_once
from commands import handle_local_command, setup_command_completion
from config import get_permission_mode
from context import RunContext
from permissions import normalize_permission_mode
from runtime import get_runtime, init_runtime
from tool_runtime import runtime_permission_setup
from tools.registry import tool_names
from tools.workspace import workspace_info
from transcript import transcripts
from ui import print_error, print_info, print_user_message, print_welcome, prompt_input
from quality_gate import run_quality_gate
from penhin_version import get_version


logger = logging.getLogger("penhin.main")


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def workspace_summary_line(info: dict[str, object] | None = None) -> str:
    info = workspace_info(tool_names()) if info is None else info
    dirty = info.get("dirty_files_count")
    if dirty is None:
        dirty = "unknown"
    agents = str(bool(info.get("has_agents_md"))).lower()
    return (
        f"[workspace] branch={info.get('git_branch', '-')} "
        f"dirty={dirty} "
        f"test={info.get('test_command_hint', '-')} "
        f"agents={agents}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = sys.argv[1:] if argv is None else argv
    if args == ["help"]:
        args = ["--help"]

    parser = argparse.ArgumentParser(
        description="Penhin Code command line interface.",
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("--once", "-o", nargs="+", metavar="TEXT", help="run one prompt and exit")
    parser.add_argument("--sessions", "-s", action="store_true", help="list saved sessions")
    parser.add_argument("--new", "-n", action="store_true", help="start without resuming history")
    parser.add_argument("--inspect-session", "-i", metavar="ID", help="show details for a session")
    parser.add_argument("--events", "-e", type=non_negative_int, default=8, metavar="N", help="number of inspect events to show")
    parser.add_argument("--resume", "-r", metavar="ID", help="resume a specific session")
    parser.add_argument("--quality-gate", action="store_true", help="run syntax, diff, and test quality gates")
    parser.add_argument("--model", metavar="MODEL", help="use a model for this session without changing saved configuration")
    parser.add_argument("--provider", choices=("anthropic", "openai", "gemini"), help="use a Provider for this session without changing saved configuration")
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()

    if args.model:
        os.environ["MODEL_ID"] = args.model
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    if args.sessions:
        print_session_list()
        return

    if args.inspect_session:
        print_session_inspect(args.inspect_session, event_limit=args.events)
        return

    if args.quality_gate:
        failures = []
        for check in run_quality_gate():
            printer = print_info if check.passed else print_error
            printer(f"[quality] {'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
            if not check.passed:
                failures.append(check)
        if failures:
            raise SystemExit(1)
        return

    init_runtime()
    logger.info(workspace_summary_line())

    if args.once:
        print_user_message(" ".join(args.once))
        run_once(" ".join(args.once))
        return
    
    if args.new:
        messages, session_path = transcripts.load_session(resume=False)
    elif args.resume:
        messages, session_path = transcripts.load_session(resume=True, session_ref=args.resume)
    else:
        messages, session_path = transcripts.load_session(resume=True)

    command_completer = setup_command_completion()
    permission_mode = get_permission_mode()
    try:
        normalize_permission_mode(permission_mode)
        policy, approval = runtime_permission_setup(permission_mode)
    except ValueError:
        permission_mode = "default"
        policy, approval = runtime_permission_setup(permission_mode)
    logger.info(f"[permission] mode={permission_mode}")
    context = RunContext(
        messages=messages,
        policy=policy,
        approval=approval,
        session_path=session_path,
    )
    workspace = workspace_info()
    provider = os.getenv("LLM_PROVIDER", "").strip().lower() or "anthropic"
    api_label = {"anthropic": "Anthropic API", "openai": "OpenAI API", "gemini": "Gemini API"}.get(provider, provider or "Configured API")
    print_welcome(
        version=get_version(),
        api=api_label,
        model=get_runtime().model,
        workspace=str(workspace.get("cwd", ".")),
    )

    while True:
        try:
            user_input = prompt_input(completer=command_completer).strip()
            
            if user_input.startswith("/"):
                handled = handle_local_command(user_input, context)
                if handled:
                    print_info("")
                    continue
        except (EOFError, KeyboardInterrupt):
            logger.info("")
            break

        if user_input in {"", "q", "quit", "exit"}:
            break

        context.add_user_message(user_input)
        print_user_message(user_input)
        agent_loop(context)
        context.session_path = transcripts.save_session(context.session_path, context.messages)


def run_cli() -> int:
    """Run the CLI without surfacing normal terminal-exit signals as errors."""
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        return 0
    return 0


def print_session_list() -> None:
    sessions = sorted(
        transcripts.list(),
        key=lambda session: session.updated_at,
        reverse=True
    )
    if not sessions:
        print("No sessions found.")
        return

    latest_path = transcripts.latest()
    print("mark | id | updated | msgs | request")
    for session in sessions:
        updated = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(session.updated_at),
        )
        first_user = session.first_user or "-"
        mark = "*" if latest_path is not None and session.path == latest_path else " "
        print(f"{mark} | {session.id[:12]} | {updated} | {session.message_count} | {first_user}")


def print_session_inspect(session_ref: str, event_limit: int = 8) -> None:
    try:
        session = transcripts.inspect(session_ref, event_limit=event_limit)
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
    print(f"last_user: {session.last_user or '-'}")
    print(f"last_assistant: {session.last_assistant or '-'}")
    print(f"tool_results: {session.tool_result_count}")
    print(f"failed_tool_results: {session.failed_tool_result_count}")
    print(f"events: {len(session.recent_events)} of {session.event_count}")
    for event in session.recent_events:
        print(f"- {event}")


if __name__ == "__main__":
    sys.exit(run_cli())
