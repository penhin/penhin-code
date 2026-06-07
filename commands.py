from dataclasses import dataclass
from typing import Callable

from tools import workspace_info
import ui


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
