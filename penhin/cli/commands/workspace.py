"""Workspace command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/workspace", "Show workspace summary", _handlers.handle_workspace_command),
)
