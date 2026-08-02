"""Authentication command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/login", "Configure provider authentication", _handlers.handle_login_command),
    CommandSpec("/logout", "Remove a stored provider credential", _handlers.handle_logout_command),
    CommandSpec("/auth", "Show authentication status", _handlers.handle_auth_command),
)
