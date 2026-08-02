"""Role-specific child-agent policy and prompt profiles."""

from .service import agent_config

SUPPORTED_PROFILES = ("general", "explore", "plan", "verification")


def profile(agent_type: str):
    return agent_config(agent_type) if agent_type in SUPPORTED_PROFILES else None


__all__ = ["SUPPORTED_PROFILES", "profile"]
