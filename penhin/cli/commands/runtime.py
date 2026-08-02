"""Runtime and provider command registrations."""

from . import _handlers
from .types import CommandSpec


COMMANDS = (
    CommandSpec("/status", "Show session and runtime status", _handlers.handle_status_command),
    CommandSpec("/model", "Show or set model id", _handlers.handle_model_command),
    CommandSpec("/provider", "Show or switch provider", _handlers.handle_provider_command),
    CommandSpec("/circuit", "Show circuit breaker status", _handlers.handle_circuit_command),
)
