"""Conversation-state command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/compact", "Compact current session, optionally with a hint", _handlers.handle_compact_command),
    CommandSpec("/force-snip", "Mark selected history turns as snipped", _handlers.handle_force_snip_command),
)
