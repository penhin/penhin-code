import pytest

from penhin.plugins.contributions import HookOutcome, PluginCommand, PluginContributions, PluginSkill
from penhin.result import Result
from penhin.cli.commands.types import CommandSpec
from penhin.cli.commands.router import CommandRouter


def test_plugin_skills_commands_and_observer_hooks_are_additive() -> None:
    contributions = PluginContributions({"/help", "/status"}, {"read", "write"}, plugin_id="sample")
    contributions.add_skill(PluginSkill("sample__guide", "A sample guide", "Use the sample workflow."))
    contributions.add_command(PluginCommand("/sample__run", "Run sample", lambda _args: Result.success("done")))
    contributions.add_hook("before_tool", lambda payload: HookOutcome(context=[f"observed {payload['name']}"]))

    outcome = contributions.observe("before_tool", {"name": "read"})
    assert contributions.skills["sample__guide"].description == "A sample guide"
    assert contributions.commands["/sample__run"].handler([]).ok
    assert outcome.context == ["observed read"]


def test_hooks_can_block_but_cannot_replace_core_commands_or_tools() -> None:
    contributions = PluginContributions({"/help"}, {"read", "write"}, plugin_id="sample")
    with pytest.raises(ValueError, match="conflicts"):
        contributions.add_command(PluginCommand("/help", "Override", lambda _args: Result.success("bad")))
    with pytest.raises(ValueError, match="core tool"):
        contributions.add_command(PluginCommand("/read", "Override", lambda _args: Result.success("bad")))
    contributions.add_hook("before_tool", lambda _payload: HookOutcome(blocked=True, reason="policy"))
    outcome = contributions.observe("before_tool", {"name": "write"})
    assert outcome.blocked and outcome.reason == "policy"


def test_plugin_command_is_discoverable_in_the_existing_command_router() -> None:
    contributions = PluginContributions({"/help"}, {"read"}, plugin_id="sample")
    contributions.add_command(PluginCommand("/sample__run", "Run sample", lambda _args: Result.success("ran")))
    router = CommandRouter(contributions=contributions)
    assert "/sample__run" in router.command_names


def test_bound_plugin_contributions_require_plugin_id_namespaces() -> None:
    contributions = PluginContributions({"/help"}, {"read"}, plugin_id="speech")
    contributions.add_skill(PluginSkill("speech__guide", "Guide", "Use it."))
    contributions.add_command(PluginCommand("/speech__transcribe", "Transcribe", lambda _args: Result.success()))
    with pytest.raises(ValueError, match="namespaced"):
        contributions.add_skill(PluginSkill("guide", "Guide", "Use it."))
    with pytest.raises(ValueError, match="namespaced"):
        contributions.add_command(PluginCommand("/transcribe", "Transcribe", lambda _args: Result.success()))


def test_command_router_rejects_a_plugin_contribution_that_replaces_a_builtin() -> None:
    contributions = PluginContributions({"/help"}, {"read"}, plugin_id="help")
    contributions.add_command(PluginCommand("/help__run", "Run", lambda _args: Result.success()))
    with pytest.raises(ValueError, match="cannot replace"):
        CommandRouter(commands=(CommandSpec("/help__run", "Built-in", lambda *_: None),), contributions=contributions)
