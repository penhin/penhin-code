from __future__ import annotations

from prompt_toolkit.completion import Completer, FuzzyCompleter, WordCompleter

from penhin.agent.context import RunContext
from penhin.cli import ui
from penhin.plugins.contributions import PluginContributions
from penhin.result import Result
from penhin.skills import load_skill

from .auth import COMMANDS as AUTH_COMMANDS
from .permissions import COMMANDS as PERMISSION_COMMANDS
from .runtime import COMMANDS as RUNTIME_COMMANDS
from .session import COMMANDS as SESSION_COMMANDS
from .types import CommandSpec
from .workspace import COMMANDS as WORKSPACE_COMMANDS
from .plugins import handle_plugin_command


class CommandRouter:
    """The only public dispatcher for interactive slash commands."""

    def __init__(
        self,
        commands: tuple[CommandSpec, ...] | None = None,
        contributions: PluginContributions | None = None,
    ):
        registered = commands or (
            *WORKSPACE_COMMANDS,
            *PERMISSION_COMMANDS,
            CommandSpec("/help", "Show local commands", self._help),
            *RUNTIME_COMMANDS,
            *AUTH_COMMANDS,
            *SESSION_COMMANDS,
            CommandSpec("/plugin", "Manage and activate plugins for this session", handle_plugin_command),
        )
        if contributions is not None:
            existing_names = {command.name for command in registered}
            conflicting = existing_names & set(contributions.commands)
            if conflicting:
                raise ValueError(f"Plugin commands cannot replace existing commands: {sorted(conflicting)}")
            registered = (*registered, *(
                CommandSpec(
                    command.name, command.description,
                    lambda args, _context, command=command: ui.print_info(command.handler(args).message),
                )
                for command in contributions.commands.values()
            ))
        self._commands = {command.name: command for command in registered}

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(self._commands)

    def command_description(self, name: str) -> str:
        return self._commands[name].description

    @staticmethod
    def _active_skills(context: RunContext | None):
        runtime = context.plugin_runtime if context is not None else None
        skills = {}
        if runtime is not None:
            for name in runtime.active():
                generation = runtime.generation(name)
                if generation is not None and generation.contributions is not None:
                    skills.update(generation.contributions.skills)
        return skills

    def skill_descriptions(self, context: RunContext | None = None) -> dict[str, str]:
        return {
            **load_skill.list_skills(),
            **{name: skill.description for name, skill in self._active_skills(context).items()},
        }

    def skill_prompt(self, text: str, context: RunContext | None = None) -> Result | None:
        if not text.startswith("/skill:"):
            return None
        command = text.split(maxsplit=1)[0]
        name = command.removeprefix("/skill:")
        skill = self._active_skills(context).get(name)
        loaded = Result.success(skill.content) if skill is not None else load_skill(name)
        if not loaded.ok:
            return loaded
        return Result.success(f"Use the following skill for this request.\n\n{text}\n\n{loaded.message}")

    def dispatch(self, text: str, context: RunContext | None = None) -> bool:
        if not text.startswith("/"):
            return False
        command_name, *args = text.split()
        command = self._commands.get(command_name)
        if command is None:
            ui.print_error(f"Unknown command: {command_name}")
            return True
        command.handler(args, context)
        return True

    def _help(self, _args: list[str], _context: RunContext | None = None) -> None:
        for command in self._commands.values():
            ui.print_info(f"{command.name} {command.description}")


class LocalCommandCompleter(Completer):
    def __init__(self, router: CommandRouter, context: RunContext | None = None):
        self._router = router
        self._context = context

    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or any(character.isspace() for character in text):
            return
        descriptions = {
            name: self._router.command_description(name) for name in self._router.command_names
        }
        descriptions.update({
            f"/skill:{name}": f"Skill · {' '.join(description.split())}"
            for name, description in self._router.skill_descriptions(self._context).items()
        })
        names = sorted(descriptions, key=len) if text != "/" else list(descriptions)
        fuzzy = FuzzyCompleter(
            WordCompleter(names, sentence=True, meta_dict=descriptions),
            pattern=r"^[^\s/]+",
        )
        matches = list(fuzzy.get_completions(document, _complete_event))
        query = text.casefold()

        def priority(completion):
            name = completion.text.casefold()
            return 0 if name == query else 1 if name.startswith(query) else 2

        # Stable sorting preserves the library's relevance ordering within each tier.
        yield from sorted(matches, key=priority)


_router = CommandRouter()


def handle_local_command(text: str, context: RunContext | None = None) -> bool:
    return _router.dispatch(text, context)


def setup_command_completion(context: RunContext | None = None) -> LocalCommandCompleter:
    return LocalCommandCompleter(_router, context)


def resolve_skill_prompt(text: str, context: RunContext | None = None) -> Result | None:
    return _router.skill_prompt(text, context)


__all__ = ["CommandRouter", "handle_local_command", "setup_command_completion", "resolve_skill_prompt"]
