"""Conversation-state command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/session", "Show current session and tree leaf", _handlers.handle_session_command),
    CommandSpec("/tree", "Show the session tree or branch from an entry", _handlers.handle_tree_command),
    CommandSpec("/fork", "Fork the session from an entry", _handlers.handle_fork_command),
    CommandSpec("/rename", "Set the current session name", _handlers.handle_rename_command),
    CommandSpec("/compact", "Compact current session, optionally with a hint", _handlers.handle_compact_command),
    CommandSpec("/force-snip", "Mark selected history turns as snipped", _handlers.handle_force_snip_command),
)
