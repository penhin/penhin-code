import pytest

from penhin.plugins.contributions import HookOutcome, PluginCommand, PluginContributions, PluginSkill
from penhin.result import Result


def test_plugin_skills_commands_and_observer_hooks_are_additive() -> None:
    contributions = PluginContributions({"/help", "/status"}, {"read", "write"})
    contributions.add_skill(PluginSkill("sample-guide", "A sample guide", "Use the sample workflow."))
    contributions.add_command(PluginCommand("/sample", "Run sample", lambda _args: Result.success("done")))
    contributions.add_hook("before_tool", lambda payload: HookOutcome(context=[f"observed {payload['name']}"]))

    outcome = contributions.observe("before_tool", {"name": "read"})
    assert contributions.skills["sample-guide"].description == "A sample guide"
    assert contributions.commands["/sample"].handler([]).ok
    assert outcome.context == ["observed read"]


def test_hooks_can_block_but_cannot_replace_core_commands_or_tools() -> None:
    contributions = PluginContributions({"/help"}, {"read", "write"})
    with pytest.raises(ValueError, match="conflicts"):
        contributions.add_command(PluginCommand("/help", "Override", lambda _args: Result.success("bad")))
    with pytest.raises(ValueError, match="core tool"):
        contributions.add_command(PluginCommand("/read", "Override", lambda _args: Result.success("bad")))
    contributions.add_hook("before_tool", lambda _payload: HookOutcome(blocked=True, reason="policy"))
    outcome = contributions.observe("before_tool", {"name": "write"})
    assert outcome.blocked and outcome.reason == "policy"


def test_plugin_command_is_discoverable_in_the_existing_command_router() -> None:
    from penhin.cli.commands.router import CommandRouter
    contributions = PluginContributions({"/help"}, {"read"})
    contributions.add_command(PluginCommand("/sample", "Run sample", lambda _args: Result.success("ran")))
    router = CommandRouter(contributions=contributions)
    assert "/sample" in router.command_names
