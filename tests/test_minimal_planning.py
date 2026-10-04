import json
import pytest

from penhin.agent.context import RunContext
from penhin.agent.loop import build_agent_deps, run_agent_state_machine
from penhin.agent.session_manager import SessionManager
from penhin.agent.state import TerminalReason
from penhin.providers.protocols import LLMUsage
from penhin.tools.execution import runtime_permission_setup


QUESTIONS = [
    {"question": "修改范围？", "options": ["只修解析器", "统一解析器", "更换库"]},
    {"question": "兼容范围？", "options": ["Python 3.11", "Python 3.12", "Python 3.13"]},
]
PLAN = "# 实施计划\n\n保留 API，统一解析器。\n\n验证：运行解析器测试。"


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


def context_for(tmp_path, *, planning=True):
    policy, approval = runtime_permission_setup("full-access")
    context = RunContext(messages=[], policy=policy, approval=approval,
                         session_manager=SessionManager.create(tmp_path / "sessions"))
    if planning:
        from penhin.cli.commands import handle_local_command
        handle_local_command("/plan", context)
    return context


def proposal(**payload):
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}


def test_questions_collect_answers_but_only_final_approval_allows_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    choices = iter(["4", "2", "1"])
    menus = []

    def select(message, options, **kwargs):
        menus.append(options)
        return next(choices)

    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", select, raising=False)
    monkeypatch.setattr("penhin.cli.ui.prompt_text", lambda message: "保留公共 API" if message == "" else pytest.fail(message))
    context = context_for(tmp_path)
    provider = ScriptedProvider(
        [proposal(questions=QUESTIONS)],
        [call("bash", command="touch premature")],
        [proposal(content=PLAN)],
        [call("bash", command="touch implemented")],
        [{"type": "text", "text": "Done"}],
    )
    run_agent_state_machine(context, build_agent_deps(provider))
    assert not (tmp_path / "premature").exists()
    assert (tmp_path / "implemented").exists()
    assert [label for _, label in menus[0]] == QUESTIONS[0]["options"] + ["其它"]
    assert [label for _, label in menus[-1]] == ["实施计划", "计划有问题"]
    assert context.planning.approved
    assert context.planning.content == PLAN
    assert context.planning.answers == [
        {"question": QUESTIONS[0]["question"], "answer": "保留公共 API"},
        {"question": QUESTIONS[1]["question"], "answer": "Python 3.12"},
    ]
    assert context.policy.mode == "full-access"
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy, approval=context.approval, session_manager=session)
    assert restored.planning == context.planning
    assert "保留公共 API" in json.dumps(provider.requests[1]["messages"], ensure_ascii=False)


def test_feedback_requires_revised_plan_and_fresh_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    choices = iter(["2", "1"])
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: next(choices), raising=False)
    monkeypatch.setattr("penhin.cli.ui.prompt_text", lambda message: "增加兼容性验证")
    context = context_for(tmp_path)
    provider = ScriptedProvider(
        [proposal(content=PLAN)],
        [call("bash", command="touch premature")],
        [proposal(content=PLAN + "\n增加兼容性验证")],
        [call("bash", command="touch approved")],
        [{"type": "text", "text": "Done"}],
    )
    run_agent_state_machine(context, build_agent_deps(provider))
    assert not (tmp_path / "premature").exists()
    assert (tmp_path / "approved").exists()
    assert context.planning.feedback == ["增加兼容性验证"]


def test_cancelled_other_input_recovers_as_text_and_continues_remaining_questions(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: "4", raising=False)
    monkeypatch.setattr("penhin.cli.ui.prompt_text", lambda *args: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)
    provider = ScriptedProvider([proposal(questions=QUESTIONS)])
    state = run_agent_state_machine(context, build_agent_deps(provider))
    assert state.terminal_reason == TerminalReason.PLAN_SELECTION_REQUIRED
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy, approval=context.approval, session_manager=session)
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: "3", raising=False)
    restored.add_user_message("1")
    assert restored.planning.active
    assert not restored.planning.approved
    assert restored.planning.answers[0]["answer"] == "1"
    assert restored.planning.answers[1]["answer"] == "Python 3.13"
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: "1")
    followup = ScriptedProvider([proposal(content=PLAN)], [{"type": "text", "text": "Starting"}])
    run_agent_state_machine(restored, build_agent_deps(followup))
    assert restored.planning.approved
    assert len(followup.requests) == 2


@pytest.mark.parametrize("arguments", [
    {"questions": []},
    {"questions": [{"question": "范围？", "options": ["小", "大"]}]},
    {"questions": [{"question": "范围？", "options": ["小", "小", "大"]}]},
    {"questions": [{"question": "", "options": ["小", "中", "大"]}]},
    {"content": " "},
    {"content": PLAN, "questions": QUESTIONS},
])
def test_invalid_planning_input_never_reaches_user(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: pytest.fail("invalid menu"))
    context = context_for(tmp_path)
    state = run_agent_state_machine(context, build_agent_deps(ScriptedProvider([proposal(**arguments)])))
    assert state.terminal_reason == TerminalReason.ERROR
    assert context.planning.active
    assert not context.planning.approved



@pytest.mark.parametrize("choice, awaiting", [(None, "approval"), ("2", "feedback")])
def test_cancelled_final_review_preserves_approval_boundary(tmp_path, monkeypatch, choice, awaiting):
    def select(*args):
        if choice is None:
            raise KeyboardInterrupt
        return choice

    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", select)
    monkeypatch.setattr("penhin.cli.ui.prompt_text", lambda *args: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)
    state = run_agent_state_machine(context, build_agent_deps(ScriptedProvider([proposal(content=PLAN)])))
    assert state.terminal_reason == TerminalReason.PLAN_SELECTION_REQUIRED
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy, approval=context.approval, session_manager=session)
    assert restored.planning.awaiting == awaiting
    restored.add_user_message("1")
    assert restored.planning.approved == (awaiting == "approval")
    assert restored.planning.active == (awaiting == "feedback")
    if awaiting == "feedback":
        assert restored.planning.feedback == ["1"]


def test_approval_persistence_failure_keeps_writes_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)
    run_agent_state_machine(context, build_agent_deps(ScriptedProvider([proposal(content=PLAN)])))
    context.session_manager.path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            context.add_user_message("1")
        assert context.planning.active
        assert not context.planning.approved
    finally:
        context.session_manager.path.chmod(0o600)


def test_branch_and_fork_restore_pending_questions_and_answers(tmp_path, monkeypatch):
    from penhin.cli.commands import handle_local_command
    from penhin.agent.session_store import sessions

    monkeypatch.setattr(sessions, "session_dir", tmp_path / "forks")
    choices = iter(["2", None])

    def select(*args):
        value = next(choices)
        if value is None:
            raise EOFError
        return value

    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", select)
    context = context_for(tmp_path, planning=False)
    context.add_user_message("Plan a parser change")
    before_plan = context.session_manager.leaf_id
    handle_local_command("/plan", context)
    run_agent_state_machine(context, build_agent_deps(ScriptedProvider([proposal(questions=QUESTIONS)])))
    pending = context.session_manager.leaf_id
    handle_local_command(f"/tree {before_plan}", context)
    assert not context.planning.active
    handle_local_command(f"/tree {pending}", context)
    assert context.planning.active
    assert context.planning.answers == [{"question": "修改范围？", "answer": "统一解析器"}]
    handle_local_command("/fork", context)
    session = SessionManager.open(context.session_manager.path)
    restored = RunContext(messages=session.build_context(), policy=context.policy, approval=context.approval, session_manager=session)
    assert restored.planning == context.planning
    assert restored.planning.questions == QUESTIONS[1:]


def test_simple_task_uses_bash_without_planning(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: pytest.fail("simple task prompted"))
    context = context_for(tmp_path, planning=False)
    provider = ScriptedProvider([call("bash", command="echo hello > greeting.txt")], [{"type": "text", "text": "Done"}])
    run_agent_state_machine(context, build_agent_deps(provider))
    assert (tmp_path / "greeting.txt").read_text().strip() == "hello"
    assert not context.planning.active


def test_begin_planning_blocks_writes_before_questions(tmp_path, monkeypatch):
    from penhin.tools.execution import run_tool
    from penhin.tools.registry import MODEL_TOOL_CATALOG

    monkeypatch.setattr("penhin.tools.builtin.shell.WORKDIR", tmp_path)
    context = context_for(tmp_path)
    blocked = run_tool("bash", {"command": "touch premature"}, context.policy, context.approval, context, MODEL_TOOL_CATALOG)
    assert blocked.result.meta["code"] == "plan_selection_required"
    assert not (tmp_path / "premature").exists()


def test_legacy_planning_checkpoint_requires_fresh_approval(tmp_path):
    context = context_for(tmp_path)
    context.session_manager.append_entry("planning", active=False, alternatives=[], selected={"title": "Old plan"})
    restored = RunContext(messages=[], policy=context.policy, approval=context.approval, session_manager=context.session_manager)
    assert restored.planning.active
    assert not restored.planning.approved


def test_model_cannot_enter_planning_through_tools_or_structured_text(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: pytest.fail("model opened planning"))
    context = context_for(tmp_path, planning=False)
    provider = ScriptedProvider(
        [call("plan", content=PLAN)],
        [proposal(content=PLAN)],
    )
    state = run_agent_state_machine(context, build_agent_deps(provider))
    assert state.terminal_reason == TerminalReason.END_TURN
    assert not context.planning.active
    assert not context.planning.approved
    assert {tool["name"] for tool in provider.requests[0]["tools"]} == {"read", "edit", "bash"}
    result = json.loads(context.messages[-2]["content"][0]["content"])
    assert result["meta"]["code"] == "unknown_tool"


def test_planning_protocol_does_not_stream_raw_json_into_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr("penhin.cli.ui.start_assistant_message", lambda: pytest.fail("raw planning JSON streamed"))
    monkeypatch.setattr("penhin.cli.ui.prompt_plan_choice", lambda *args: (_ for _ in ()).throw(EOFError()))
    context = context_for(tmp_path)

    class StreamingProvider(ScriptedProvider):
        def call_with_retry(self, **kwargs):
            kwargs["stream_callback"]('{"content":')
            kwargs["stream_callback"]('"A plan"}')
            return super().call_with_retry(**kwargs)

    state = run_agent_state_machine(context, build_agent_deps(StreamingProvider([proposal(content=PLAN)])))
    assert state.terminal_reason == TerminalReason.PLAN_SELECTION_REQUIRED
    assert context.planning.content == PLAN
