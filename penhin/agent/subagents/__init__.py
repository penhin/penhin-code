"""Role-specific child-agent execution."""

from .profiles import SUPPORTED_PROFILES, profile
from .service import run_subagent

__all__ = ["SUPPORTED_PROFILES", "profile", "run_subagent"]
