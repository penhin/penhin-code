from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.completion import Completer, Completion

import ui

from config import get_permission_mode, set_permission_mode
from context import RunContext
from permissions import PERMISSION_MODES, PermissionMode, permission_setup, transition_mode
from tools.registry import tool_names
from tools.workspace import workspace_info


@dataclass(frozen=True)
class LocalCommand:
    name: str
    description: str
    handler: Callable[[list[str], RunContext | None], None]
    

def handle_local_command(text: str, context: RunContext | None = None) -> bool:
    if not text.startswith("/"):
        return False
    
    parts = text.split()
    command_name = parts[0]
    args = parts[1:]
    
    command = LOCAL_COMMANDS.get(command_name)
    if command is None:
        ui.print_error(f"Unknown command: {command_name}")
        return True
    
    command.handler(args, context)
    return True

    
def handle_workspace_command(args: list[str], context: RunContext | None = None):
    ui.print_json(workspace_info(tool_names()))


def handle_help_command(args: list[str], context: RunContext | None = None):
    for command in LOCAL_COMMANDS.values():
        ui.print_info(f"{command.name} {command.description}")


def handle_permission_command(args: list[str], context: RunContext | None = None):
    if not args:
        ui.print_info(f"permission: {get_permission_mode()}")
        return

    mode = args[0]
    if mode not in PERMISSION_MODES:
        ui.print_error(f"Unknown permission mode: {mode}")
        ui.print_info(f"Available modes: {', '.join(sorted(PERMISSION_MODES))}")
        return

    # Use state machine when context is available (plan mode needs save/restore).
    if context is not None:
        current = PermissionMode(get_permission_mode())
        target = PermissionMode(mode)
        transition_mode(current, target, context)

    set_permission_mode(mode)
    policy, approval = permission_setup(mode)
    if context is not None:
        context.policy = policy
        context.approval = approval
    ui.print_info(f"permission: {mode}")


def complete_local_command(text: str, state: int) -> str | None:
    matches = [
        name for name in LOCAL_COMMANDS
        if name.startswith(text)
    ]

    if state < len(matches):
        return matches[state]
    return None


class LocalCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return

        for name in LOCAL_COMMANDS:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text))


def setup_command_completion() -> LocalCommandCompleter:
    completer = LocalCommandCompleter()
    ui.set_prompt_completer(completer)
    return completer

    
LOCAL_COMMANDS = {
    "/workspace": LocalCommand(
        name="/workspace",
        description="Show workspace summary",
        handler=handle_workspace_command,
    ),
    "/permission": LocalCommand(
        name="/permission",
        description="Show or set permission mode",
        handler=handle_permission_command,
    ),
    "/perm": LocalCommand(
        name="/perm",
        description="Alias for /permission",
        handler=handle_permission_command,
    ),
    "/help": LocalCommand(
        name="/help",
        description="Show local commands",
        handler=handle_help_command,
    ),
}
