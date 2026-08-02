from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion

from penhin.agent.context import RunContext
from penhin.cli import ui

from .auth import COMMANDS as AUTH_COMMANDS
from .permissions import COMMANDS as PERMISSION_COMMANDS
from .runtime import COMMANDS as RUNTIME_COMMANDS
from .session import COMMANDS as SESSION_COMMANDS
from .types import CommandSpec
from .workspace import COMMANDS as WORKSPACE_COMMANDS


class CommandRouter:
    """The only public dispatcher for interactive slash commands."""

    def __init__(self, commands: tuple[CommandSpec, ...] | None = None):
        registered = commands or (
            *WORKSPACE_COMMANDS,
            *PERMISSION_COMMANDS,
            CommandSpec("/help", "Show local commands", self._help),
            *RUNTIME_COMMANDS,
            *AUTH_COMMANDS,
            *SESSION_COMMANDS,
        )
        self._commands = {command.name: command for command in registered}

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(self._commands)

    def dispatch(self, text: str, context: RunContext | None = None) -> bool:
        if not text.startswith("/"):
            return False
        command_name, *args = text.split()
        command = self._commands.get(command_name)
        if command is None:
            ui.print_error(f"Unknown command: {command_name}")
            return True
        command.handler(args, context)
        return True

    def _help(self, _args: list[str], _context: RunContext | None = None) -> None:
        for command in self._commands.values():
            ui.print_info(f"{command.name} {command.description}")


class LocalCommandCompleter(Completer):
    def __init__(self, router: CommandRouter):
        self._router = router

    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name in self._router.command_names:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text))


_router = CommandRouter()


def handle_local_command(text: str, context: RunContext | None = None) -> bool:
    return _router.dispatch(text, context)


def setup_command_completion() -> LocalCommandCompleter:
    return LocalCommandCompleter(_router)


__all__ = ["CommandRouter", "handle_local_command", "setup_command_completion"]
