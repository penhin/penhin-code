"""Constrained, additive plugin contributions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from penhin.result import Result


@dataclass(frozen=True)
class PluginSkill:
    name: str
    description: str
    content: str


@dataclass(frozen=True)
class PluginCommand:
    name: str
    description: str
    handler: Callable[[list[str]], Result]


@dataclass
class HookOutcome:
    context: list[str] = field(default_factory=list)
    blocked: bool = False
    reason: str = ""


class PluginContributions:
    """Additive skills, commands, and observation-only lifecycle hooks."""

    def __init__(self, core_commands: set[str], core_tools: set[str], *, plugin_id: str) -> None:
        self._core_commands = core_commands
        self._core_tools = core_tools
        self._plugin_id = plugin_id
        self.skills: dict[str, PluginSkill] = {}
        self.commands: dict[str, PluginCommand] = {}
        self.hooks: dict[str, list[Callable[[dict[str, Any]], HookOutcome]]] = {}

    def add_skill(self, skill: PluginSkill) -> None:
        if not skill.name.startswith(f"{self._plugin_id}__"):
            raise ValueError("Plugin skill must be Plugin-ID namespaced")
        if skill.name in self.skills:
            raise ValueError(f"Duplicate plugin skill: {skill.name}")
        self.skills[skill.name] = skill

    def add_command(self, command: PluginCommand) -> None:
        if not command.name.startswith("/"):
            raise ValueError("Plugin command must start with /")
        if command.name in self._core_commands or command.name in self.commands:
            raise ValueError(f"Plugin command conflicts with an existing command: {command.name}")
        if command.name.removeprefix("/") in self._core_tools:
            raise ValueError("Plugin command cannot replace a core tool")
        if not command.name.startswith(f"/{self._plugin_id}__"):
            raise ValueError("Plugin command must be Plugin-ID namespaced")
        self.commands[command.name] = command

    def add_hook(self, event: str, hook: Callable[[dict[str, Any]], HookOutcome]) -> None:
        if event not in {"before_tool", "after_tool", "before_llm"}:
            raise ValueError(f"Unsupported plugin hook event: {event}")
        self.hooks.setdefault(event, []).append(hook)

    def observe(self, event: str, payload: dict[str, Any]) -> HookOutcome:
        combined = HookOutcome()
        for hook in self.hooks.get(event, []):
            result = hook(dict(payload))
            combined.context.extend(result.context)
            if result.blocked:
                combined.blocked = True
                combined.reason = result.reason or "Blocked by plugin hook"
                break
        return combined
