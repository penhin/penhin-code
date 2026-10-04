import json
import pytest

from penhin.agent.context import RunContext
from penhin.agent.loop import build_agent_deps, run_agent_state_machine
from penhin.agent.session_manager import SessionManager
from penhin.agent.state import TerminalReason
from penhin.providers.protocols import LLMUsage
from penhin.tools.execution import runtime_permission_setup


ALTERNATIVES = [
    {"title": "Focused fix", "scope": "Fix the parser only", "risks": "Other parsers remain unchanged", "cost": "Small", "verification": "Run parser tests"},
    {"title": "Shared parser", "scope": "Unify the two parsers", "risks": "More callers affected", "cost": "Medium", "verification": "Run both parser suites"},
    {"title": "Library migration", "scope": "Replace parsing with the installed library", "risks": "Input compatibility", "cost": "Large", "verification": "Run compatibility fixtures"},
]


class ScriptedProvider:
    max_tokens = 100
    context_window = 100_000
    compaction_reserve_tokens = 1_000

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def call_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        content = next(self.responses)
        return type("Response", (), {
            "content": content, "usage": LLMUsage(input_tokens=1, output_tokens=1),
            "stop_reason": "tool_use" if any(block["type"] == "tool_use" for block in content) else "end_turn",
        })()


def call(name, **arguments):
    return {"type": "tool_use", "id": name, "name": name, "input": arguments}


def context_for(tmp_path):
    policy, approval = runtime_permission_setup("full-access")
    return RunContext(messages=[], policy=policy, approval=approval,
                      session_manager=SessionManager.create(tmp_path / "sessions"))


def test_selecting_one_of_three_plans_persists_choice_and_continues(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    selections = []

    def select(message, options, **kwargs):
        selections.append(options)
        return "2"

    monkeypatch.setattr("penhin.cli.ui.prompt_select", select)
    provider = ScriptedProvider(
        [call("plan", alternatives=ALTERNATIVES)],
        [{"type": "text", "text": "Implementing the shared parser."}],
    )
    state = run_agent_state_machine(context, build_agent_deps(provider))

    assert len(selections) == 1
    assert [value for value, _ in selections[0]] == ["1", "2", "3", "custom"]
    assert state.turn == 2
    result = next(json.loads(block["content"]) for message in context.messages
                  if isinstance(message["content"], list) for block in message["content"]
                  if block["type"] == "tool_result")
    assert result["ok"]
    assert result["data"]["selected"] == ALTERNATIVES[1]
    assert context.policy.mode == "full-access"
    restored = SessionManager.open(context.session_manager.path)
    assert any(entry["type"] == "planning" and entry["selected"] == ALTERNATIVES[1]
               for entry in restored.branch_entries())


def test_custom_suggestion_requires_three_revised_options_before_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    choices = iter(["custom", "1"])
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr("penhin.cli.ui.prompt_text", lambda *args: "Keep the API stable")
    context = context_for(tmp_path)
    revised = [{**option, "scope": option["scope"] + " with the public API preserved"} for option in ALTERNATIVES]
    provider = ScriptedProvider(
        [call("plan", alternatives=ALTERNATIVES), call("bash", command="touch premature")],
        [call("plan", alternatives=revised)],
        [call("bash", command="touch implemented")],
        [{"type": "text", "text": "Done"}],
    )

    run_agent_state_machine(context, build_agent_deps(provider))

    assert not (tmp_path / "premature").exists()
    assert (tmp_path / "implemented").exists()
    assert context.planning.selected == revised[0]
    assert "Keep the API stable" in json.dumps(provider.requests[1]["messages"])


@pytest.mark.parametrize("alternatives", [ALTERNATIVES[:2], ALTERNATIVES + ALTERNATIVES[:1],
                                          [ALTERNATIVES[0]] * 3,
                                          [{**option, "verification": ""} for option in ALTERNATIVES]])
def test_invalid_plans_do_not_reach_user_selection(tmp_path, monkeypatch, alternatives):
    from penhin.tools.execution import run_tool
    from penhin.tools.registry import MODEL_TOOL_CATALOG

    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: pytest.fail("invalid plan displayed"))
    context = context_for(tmp_path)
    result = run_tool("plan", {"alternatives": alternatives}, context.policy, context.approval,
                      context, MODEL_TOOL_CATALOG).result
    assert not result.ok
    assert result.meta["code"] == "invalid_tool_input"


def test_cancelled_selection_pauses_and_survives_session_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    context = context_for(tmp_path)
    provider = ScriptedProvider([call("plan", alternatives=ALTERNATIVES)])

    state = run_agent_state_machine(context, build_agent_deps(provider))

    assert state.terminal_reason == TerminalReason.PLAN_SELECTION_REQUIRED
    assert len(provider.requests) == 1
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy,
                          approval=context.approval, session_manager=session)
    assert restored.planning.active
    assert restored.planning.alternatives == ALTERNATIVES
    restored.add_user_message("3")
    followup = ScriptedProvider([{"type": "text", "text": "Starting the library migration."}])
    run_agent_state_machine(restored, build_agent_deps(followup))
    assert restored.planning.selected == ALTERNATIVES[2]
    assert not restored.planning.active


def test_simple_task_uses_bash_without_planning(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: pytest.fail("simple task prompted"))
    context = context_for(tmp_path)
    provider = ScriptedProvider([call("bash", command="echo hello > greeting.txt")],
                                [{"type": "text", "text": "Created greeting.txt"}])
    run_agent_state_machine(context, build_agent_deps(provider))
    assert (tmp_path / "greeting.txt").read_text().strip() == "hello"
    assert not context.planning.active


def test_enter_planning_blocks_implementation_before_options_exist(tmp_path, monkeypatch):
    from penhin.tools.execution import run_tool
    from penhin.tools.registry import MODEL_TOOL_CATALOG

    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    context = context_for(tmp_path)
    assert run_tool("plan", {}, context.policy, context.approval, context, MODEL_TOOL_CATALOG).result.ok
    blocked = run_tool("bash", {"command": "touch premature"}, context.policy, context.approval, context, MODEL_TOOL_CATALOG)
    assert blocked.result.meta["code"] == "plan_selection_required"
    assert not (tmp_path / "premature").exists()
    assert context.policy.mode == "full-access"


def test_selection_persistence_failure_keeps_implementation_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)
    run_agent_state_machine(context, build_agent_deps(ScriptedProvider([call("plan", alternatives=ALTERNATIVES)])))
    context.session_manager.path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            context.add_user_message("1")
        assert context.planning.active
        assert context.planning.selected is None
    finally:
        context.session_manager.path.chmod(0o600)


def test_branch_and_fork_restore_the_target_planning_gate(tmp_path, monkeypatch):
    from penhin.cli.commands import handle_local_command
    from penhin.agent.session_store import sessions

    monkeypatch.setattr(sessions, "session_dir", tmp_path / "forks")
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)
    context.add_user_message("Plan a parser change")
    before_plan = context.session_manager.leaf_id
    run_agent_state_machine(context, build_agent_deps(ScriptedProvider([call("plan", alternatives=ALTERNATIVES)])))
    pending = context.session_manager.leaf_id
    context.add_user_message("1")

    handle_local_command(f"/tree {before_plan}", context)
    assert not context.planning.active
    assert context.planning.selected is None
    handle_local_command(f"/tree {pending}", context)
    assert context.planning.active
    assert context.planning.selected is None
    handle_local_command("/fork", context)
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy,
                          approval=context.approval, session_manager=session)
    assert restored.planning.active
    assert restored.planning.alternatives == ALTERNATIVES



def test_material_alternatives_can_address_the_same_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_select", lambda *args, **kwargs: "1")
    context = context_for(tmp_path)
    alternatives = [{**option, "scope": "Support JSON and TOML configuration"} for option in ALTERNATIVES]
    provider = ScriptedProvider([call("plan", alternatives=alternatives)],
                                [{"type": "text", "text": "Starting"}])
    run_agent_state_machine(context, build_agent_deps(provider))
    assert context.planning.selected == alternatives[0]
