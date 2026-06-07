from dataclasses import dataclass
from typing import Callable

from tools import workspace_info
import ui

try:
    import readline
except ImportError:
    readline = None

@dataclass(frozen=True)
class LocalCommand:
    name: str
    description: str
    handler: Callable[[list[str]], None]
    

def handle_local_command(text: str) -> bool:
    if not text.startswith("/"):
        return False
    
    parts = text.split()
    command_name = parts[0]
    args = parts[1:]
    
    command = LOCAL_COMMANDS.get(command_name)
    if command is None:
        ui.print_error(f"Unknown command: {command_name}")
        return True
    
    command.handler(args)
    return True

    
def handle_workspace_command(args: list[str]):
    ui.print_json(workspace_info())


def handle_help_command(args: list[str]):
    for command in LOCAL_COMMANDS.values():
        ui.print_info(f"{command.name} {command.description}")


def complete_local_command(text: str, state: int) -> str | None:
    matches = [
        name for name in LOCAL_COMMANDS
        if name.startswith(text)
    ]

    if state < len(matches):
        return matches[state]
    return None


def setup_command_completion() -> None:
    if readline is None:
        return

    readline.set_completer(complete_local_command)
    if hasattr(readline, "set_completer_delims"):
        readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    
LOCAL_COMMANDS = {
    "/workspace": LocalCommand(
        name="/workspace",
        description="Show workspace summary",
        handler=handle_workspace_command,
    ),
    "/help": LocalCommand(
        name="/help",
        description="Show local commands",
        handler=handle_help_command,
    ),
}
