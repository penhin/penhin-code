import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.agent.subagents import service as subagent
from penhin.agent.state import AgentDeps, AgentPhase, TerminalReason
from penhin.runtime.retry import CircuitBreakerOpen
from penhin.agent.context import RunContext
from penhin.agent.prompts import (
    build_exploration_system,
    build_plan_agent_system,
    build_subagent_system,
    build_verification_system,
)
from penhin.providers.protocols import LLMUsage
from penhin.result import Result
from penhin.tools.task_state import TaskStatusManager
from penhin.tools import TOOL_SPECS
from penhin.tools.builtin import tasks as task_tools
from penhin.tools.builtin.plans import write_plan


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class RecordingRuntime:
    sub_max_tokens = 123
    sub_max_turns = 1

    def __init__(self):
        self.calls = []

    def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse([{"type": "text", "text": "done"}])


class CircuitOpenRuntime:
    sub_max_tokens = 123
    sub_max_turns = 1

    def call_with_retry(self, **kwargs):
        raise CircuitBreakerOpen("open")


class EmptyThenSummaryRuntime:
    sub_max_tokens = 123
    sub_max_turns = 1

    def __init__(self):
        self.calls = []
        self.responses = [
            FakeResponse(
                [{"type": "thinking", "thinking": "internal notes"}],
                usage=LLMUsage(input_tokens=10, output_tokens=123),
            ),
            FakeResponse([{"type": "text", "text": "fallback summary"}]),
        ]

    def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class MaxTokensThenSummaryRuntime:
    sub_max_tokens = 123
    sub_max_turns = 1

    def __init__(self):
        self.calls = []
        self.responses = [
            FakeResponse(
                [{"type": "text", "text": "partial report"}],
                stop_reason="max_tokens",
                usage=LLMUsage(input_tokens=100, output_tokens=123),
            ),
            FakeResponse([{"type": "text", "text": "complete compressed report"}]),
        ]

    def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ToolBudgetThenSummaryRuntime:
    sub_max_tokens = 123
    sub_max_turns = 30

    def __init__(self):
        self.calls = []
        self.responses = [
            FakeResponse(
                [
                    {
                        "type": "tool_use",
                        "id": f"read-{index}",
                        "name": "read",
                        "input": {"path": "README.md"},
                    }
                    for index in range(10)
                ],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=100, output_tokens=50),
            ),
            FakeResponse([{"type": "text", "text": "budgeted summary"}]),
        ]

    def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_run_subagent_defaults_to_general_type() -> None:
    runtime = RecordingRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("say done")

    assert result.ok is True
    assert result.message == "done"
    assert runtime.calls[0]["system"] == build_subagent_system()


def test_run_subagent_uses_verification_type() -> None:
    runtime = RecordingRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("verify work", agent_type="verification")

    tool_names = {tool["name"] for tool in runtime.calls[0]["tools"]}
    assert result.ok is True
    assert runtime.calls[0]["system"] == build_verification_system()
    assert {"bash", "read", "search", "task_show", "todo_show"} <= tool_names
    assert "write" not in tool_names
    assert "edit" not in tool_names
    assert "task" not in tool_names
    assert "task_complete" not in tool_names


def test_run_subagent_uses_exploration_type() -> None:
    runtime = RecordingRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("find relevant files", agent_type="explore")

    tool_names = {tool["name"] for tool in runtime.calls[0]["tools"]}
    assert result.ok is True
    assert runtime.calls[0]["system"] == build_exploration_system()
    assert tool_names == {"glob", "list", "read", "search", "workspace"}
    assert "bash" not in tool_names
    assert "write" not in tool_names
    assert "edit" not in tool_names
    assert "task" not in tool_names
    assert "task_show" not in tool_names
    assert runtime.calls[0]["max_tokens"] == 800


def test_exploration_config_has_tight_turn_budget() -> None:
    config = subagent.agent_config("explore")

    assert config is not None
    assert config["max_turns"] == 6
    assert config["max_tokens"] == 800
    assert config["final_max_tokens"] == 2000
    assert config["max_tool_calls"] == 8


def test_run_subagent_uses_plan_type() -> None:
    runtime = RecordingRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("design implementation plan", agent_type="plan")

    tool_names = {tool["name"] for tool in runtime.calls[0]["tools"]}
    assert result.ok is True
    assert runtime.calls[0]["system"] == build_plan_agent_system()
    assert runtime.calls[0]["system"].startswith("You are a software architect.")
    assert "penhin.dag/v1" in runtime.calls[0]["system"]
    assert tool_names == {"glob", "list", "read", "search", "workspace"}
    assert "bash" not in tool_names
    assert "write" not in tool_names
    assert "edit" not in tool_names
    assert "task" not in tool_names


def test_subagent_initial_messages_are_isolated_to_task_and_project_instructions() -> None:
    messages = subagent.build_subagent_initial_messages("inspect routing")

    assert messages[-1] == {"role": "user", "content": "inspect routing"}
    assert all("parent secret" not in str(message.get("content", "")) for message in messages)
    assert all(message.get("role") == "user" for message in messages)


def test_run_subagent_rejects_unknown_type() -> None:
    result = subagent.run_subagent("work", agent_type="missing")

    assert result.ok is False
    assert result.meta["code"] == "unknown_agent_type"
    assert result.data["agent_type"] == "missing"
    assert result.data["available"] == ["explore", "general", "plan", "verification"]


def test_run_subagent_returns_failure_when_circuit_is_open() -> None:
    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=CircuitOpenRuntime()):
        result = subagent.run_subagent("say done")

    assert result.ok is False
    assert result.meta["code"] == "circuit_open"
    assert result.error == subagent.API_UNAVAILABLE_MESSAGE


def test_run_subagent_requests_final_summary_when_final_response_has_no_text() -> None:
    runtime = EmptyThenSummaryRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("summarize findings", agent_type="explore")

    assert result.ok is True
    assert result.message == "fallback summary"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["tools"]
    assert "tools" not in runtime.calls[1]
    assert runtime.calls[1]["max_tokens"] == 2000


def test_run_subagent_requests_final_summary_when_final_response_hits_max_tokens() -> None:
    runtime = MaxTokensThenSummaryRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("summarize findings", agent_type="explore")

    assert result.ok is True
    assert result.message == "complete compressed report"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["tools"]
    assert "tools" not in runtime.calls[1]


def test_run_subagent_summarizes_immediately_when_tool_budget_is_exhausted() -> None:
    runtime = ToolBudgetThenSummaryRuntime()

    with patch("penhin.agent.subagents.service.runtime_manager.current", return_value=runtime), patch("penhin.agent.subagents.service.log_usage"):
        result = subagent.run_subagent("summarize findings", agent_type="explore")

    assert result.ok is True
    assert result.message == "budgeted summary"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["tools"]
    assert "tools" not in runtime.calls[1]


def test_run_subagent_state_machine_stops_at_max_turns_after_tool_results() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "inspect"}],
        policy=subagent.PermissionPolicy(allow=set()),
        approval=subagent.ApprovalFlow.preapproved(set()),
    )
    events = []
    budget_exhausted = {"value": False}
    response = FakeResponse([], stop_reason="tool_use")
    tool_results = [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}]

    deps = AgentDeps(
        compact_context=lambda ctx: events.append("compact"),
        call_llm=lambda ctx: response,
        record_llm_response=lambda ctx, resp: ctx.add_assistant_message(resp.content),
        should_continue_with_tools=lambda resp: True,
        execute_tool_uses=lambda ctx, resp: (tool_results, False),
        record_tool_results=lambda ctx, results, manual: ctx.add_tool_results(results),
        handle_circuit_open=lambda ctx, error: events.append("circuit"),
    )

    state = subagent.run_subagent_state_machine(context, deps, max_turns=1, budget_exhausted=budget_exhausted)

    assert events == ["compact"]
    assert state.phase == AgentPhase.FINISHED
    assert state.terminal_reason == TerminalReason.MAX_TURNS
    assert state.turn == 1
    assert context.messages[-1] == {"role": "user", "content": tool_results}


def test_task_tool_exposes_limited_agent_types() -> None:
    schema = TOOL_SPECS["task"].input_schema

    assert schema["properties"]["agent_type"]["enum"] == ["explore", "general", "plan"]
    assert schema["required"] == ["task"]


def test_run_task_uses_general_subagent() -> None:
    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("ok")) as mocked_run_subagent:
        result = task_tools.run_task("inspect work")

    assert result.ok is True
    mocked_run_subagent.assert_called_once_with("inspect work", agent_type="general")


def test_run_task_uses_requested_subagent_type() -> None:
    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("ok")) as mocked_run_subagent:
        result = task_tools.run_task("inspect work", agent_type="explore")

    assert result.ok is True
    mocked_run_subagent.assert_called_once_with("inspect work", agent_type="explore")


def test_task_tool_handler_uses_general_subagent() -> None:
    handler = TOOL_SPECS["task"].handler
    assert handler is not None

    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("ok")) as mocked_run_subagent:
        result = handler(task="inspect work")

    assert result.ok is True
    mocked_run_subagent.assert_called_once_with("inspect work", agent_type="general")


def test_task_tool_handler_uses_requested_subagent_type() -> None:
    handler = TOOL_SPECS["task"].handler
    assert handler is not None

    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("ok")) as mocked_run_subagent:
        result = handler(task="inspect work", agent_type="explore")

    assert result.ok is True
    mocked_run_subagent.assert_called_once_with("inspect work", agent_type="explore")


def test_run_verify_uses_verification_subagent() -> None:
    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("verified")) as mocked_run_subagent:
        result = task_tools.run_verify(
            goal="add verification tool",
            plan="wire tool registry",
            changes="updated tools/tasks.py",
            test_hint=".venv/bin/python tests/test_smoke.py",
        )

    assert result.ok is True
    prompt = mocked_run_subagent.call_args.args[0]
    assert "Goal:\nadd verification tool" in prompt
    assert "Plan:\nwire tool registry" in prompt
    assert "Changes:\nupdated tools/tasks.py" in prompt
    assert "Suggested checks:\n.venv/bin/python tests/test_smoke.py" in prompt
    mocked_run_subagent.assert_called_once()
    assert mocked_run_subagent.call_args.kwargs["agent_type"] == "verification"


def test_verify_tool_handler_uses_verification_subagent() -> None:
    handler = TOOL_SPECS["verify"].handler
    assert handler is not None

    with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("verified")) as mocked_run_subagent:
        result = handler(goal="finish work", test_hint="run smoke")

    assert result.ok is True
    assert "finish work" in mocked_run_subagent.call_args.args[0]
    assert mocked_run_subagent.call_args.kwargs["agent_type"] == "verification"


def test_run_verify_loads_current_task_plan_slug() -> None:
    original_task_status = task_tools.task_status
    slug = "verify-associated-plan-test"
    plan_path = write_plan("1. implement verify\n2. run smoke", slug=slug)

    try:
        manager = TaskStatusManager(Path.cwd() / ".tasks-test-verify")
        task_tools.task_status = manager
        manager.start("verify work", plan_slug=slug)

        with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("verified")) as mocked_run_subagent:
            result = task_tools.run_verify(goal="confirm verification")

        prompt = mocked_run_subagent.call_args.args[0]
        assert result.ok is True
        assert f"Plan ({slug}):\n1. implement verify\n2. run smoke" in prompt
        assert mocked_run_subagent.call_args.kwargs["agent_type"] == "verification"
        shown = manager.show()
        assert shown.verified_plan_slug == slug
    finally:
        task_tools.task_status = original_task_status
        plan_path.unlink(missing_ok=True)
        test_tasks_dir = Path.cwd() / ".tasks-test-verify"
        for path in test_tasks_dir.glob("*"):
            path.unlink()
        test_tasks_dir.rmdir()


def test_run_verify_loads_explicit_plan_slug() -> None:
    original_task_status = task_tools.task_status
    slug = "verify-explicit-slug-test"
    plan_path = write_plan("1. load explicit slug\n2. verify", slug=slug)

    try:
        manager = TaskStatusManager(Path.cwd() / ".tasks-test-explicit-slug")
        task_tools.task_status = manager
        manager.start("verify work", plan_slug="different-current-plan")

        with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("verified")) as mocked_run_subagent:
            result = task_tools.run_verify(
                goal="confirm verification",
                plan_slug=slug,
            )

        prompt = mocked_run_subagent.call_args.args[0]
        assert result.ok is True
        assert f"Plan ({slug}):\n1. load explicit slug\n2. verify" in prompt
        assert manager.show().verified_plan_slug == slug
    finally:
        task_tools.task_status = original_task_status
        plan_path.unlink(missing_ok=True)
        test_tasks_dir = Path.cwd() / ".tasks-test-explicit-slug"
        for path in test_tasks_dir.glob("*"):
            path.unlink()
        test_tasks_dir.rmdir()


def test_run_verify_explicit_plan_overrides_current_task_plan_slug() -> None:
    original_task_status = task_tools.task_status
    slug = "verify-explicit-plan-test"
    plan_path = write_plan("associated plan should not appear", slug=slug)

    try:
        manager = TaskStatusManager(Path.cwd() / ".tasks-test-explicit-verify")
        task_tools.task_status = manager
        manager.start("verify work", plan_slug=slug)

        with patch("penhin.orchestration.service.run_recorded_subagent", return_value=Result.success("verified")) as mocked_run_subagent:
            result = task_tools.run_verify(
                goal="confirm verification",
                plan="explicit plan wins",
            )

        prompt = mocked_run_subagent.call_args.args[0]
        assert result.ok is True
        assert "Plan:\nexplicit plan wins" in prompt
        assert "associated plan should not appear" not in prompt
    finally:
        task_tools.task_status = original_task_status
        plan_path.unlink(missing_ok=True)
        test_tasks_dir = Path.cwd() / ".tasks-test-explicit-verify"
        for path in test_tasks_dir.glob("*"):
            path.unlink()
        test_tasks_dir.rmdir()
