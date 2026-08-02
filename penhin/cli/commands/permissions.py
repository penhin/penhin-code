"""Permission command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/permission", "Show or set permission mode", _handlers.handle_permission_command),
)
