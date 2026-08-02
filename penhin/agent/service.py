from __future__ import annotations

from penhin.result import Result

from .context import RunContext
from .loop import agent_loop, run_once
from .state import AgentState
from .subagents.service import run_subagent


class AgentService:
    """Stable entry point for parent and role-based child agent execution."""

    def run(self, context: RunContext) -> AgentState:
        return agent_loop(context)

    def run_once(self, context: RunContext) -> AgentState:
        return run_once(context)

    def run_child(self, task: str, agent_type: str = "general") -> Result:
        return run_subagent(task, agent_type)


agent_service = AgentService()


__all__ = ["AgentService", "agent_service"]
