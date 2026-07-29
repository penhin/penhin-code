import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
from agent_state import AgentDeps, AgentPhase, AgentState, TerminalReason, step_agent
from circuit_breaker import CircuitBreakerOpen
from context import RunContext
from result import Result
from tool_runtime import ApprovalFlow, PermissionPolicy, ToolRun


def empty_policy() -> PermissionPolicy:
    return PermissionPolicy(allow=set(), deny=set())


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


class FakeToolBlock:
    type = "tool_use"

    def __init__(self, name="read", tool_input=None, block_id="tool-1"):
        self.name = name
        self.input = {} if tool_input is None else tool_input
        self.id = block_id


class FakeRuntime:
    max_tokens = 100

    def call_with_retry(self, **kwargs):
        return FakeResponse([{"type": "text", "text": "done"}])


class RecordingRuntime:
    max_tokens = 123

    def __init__(self):
        self.kwargs = None

    def call_with_retry(self, **kwargs):
        self.kwargs = kwargs
        return FakeResponse([{"type": "text", "text": "done"}])


class CircuitOpenRuntime:
    max_tokens = 100

    def call_with_retry(self, **kwargs):
        raise CircuitBreakerOpen("open")


def test_resolve_approval_approves_for_session() -> None:
    approval = ApprovalFlow.require_confirmation({"write"})
    policy = PermissionPolicy(allow={"write"}, deny=set())
    tool_input = {"path": "demo.txt", "content": "hello"}

    with patch("builtins.input", return_value="ys"), patch("agent.run_tool") as mocked_run_tool:
        mocked_run_tool.return_value = ToolRun(Result.success("ok"))
        tool_run = agent.resolve_approval("write", tool_input, policy, approval)

    assert tool_run.result.message == "ok"
    assert approval.is_approved("write", tool_input)
    mocked_run_tool.assert_called_once()


def test_resolve_approval_approves_bash_prefix_for_session() -> None:
    approval = ApprovalFlow.require_confirmation({"bash"})
    policy = PermissionPolicy(allow={"bash"}, deny=set())
    tool_input = {"command": "pytest tests/test_agent.py -q"}

    with patch("builtins.input", return_value="3"), patch("agent.run_tool") as mocked_run_tool:
        mocked_run_tool.return_value = ToolRun(Result.success("ok"))
        tool_run = agent.resolve_approval("bash", tool_input, policy, approval)

    assert tool_run.result.message == "ok"
    assert approval.is_approved("bash", {"command": "pytest tests/test_tools.py -q"})
    mocked_run_tool.assert_called_once()


def test_resolve_approval_rejects_when_input_is_unavailable() -> None:
    approval = ApprovalFlow.require_confirmation({"write"})
    policy = PermissionPolicy(allow={"write"}, deny=set())
    tool_input = {"path": "demo.txt", "content": "hello"}

    with patch("builtins.input", side_effect=EOFError), patch("agent.run_tool") as mocked_run_tool:
        mocked_run_tool.return_value = ToolRun(Result.failure("rejected"))
        tool_run = agent.resolve_approval("write", tool_input, policy, approval)

    assert tool_run.result.ok is False
    assert approval.is_rejected("write", tool_input) is False
    rejected_approval = mocked_run_tool.call_args.args[3]
    assert rejected_approval.is_rejected("write", tool_input)


def test_agent_loop_updates_run_context_messages() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    with patch("agent.get_runtime", return_value=FakeRuntime()), patch("agent.log_usage"):
        agent.agent_loop(context)

    assert context.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]


def test_agent_loop_prepares_context_before_llm_call() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    with (
        patch("agent.get_runtime", return_value=FakeRuntime()),
        patch("agent.log_usage"),
        patch("agent.compact_context_for_llm") as mocked_compact,
    ):
        agent.agent_loop(context)

    mocked_compact.assert_called_once_with(context)


def test_agent_loop_records_message_when_circuit_is_open() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    with patch("agent.get_runtime", return_value=CircuitOpenRuntime()):
        agent.agent_loop(context)

    assert context.messages[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": agent.API_UNAVAILABLE_MESSAGE}],
    }


def test_agent_loop_returns_terminal_state() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    with patch("agent.get_runtime", return_value=FakeRuntime()), patch("agent.log_usage"):
        state = agent.agent_loop(context)

    assert state.phase == AgentPhase.FINISHED
    assert state.terminal_reason == TerminalReason.END_TURN
    assert state.turn == 1
    assert state.last_stop_reason == "end_turn"


def test_call_llm_uses_run_context_messages() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    runtime = RecordingRuntime()

    response = agent.call_llm(context, runtime)

    assert response.content == [{"type": "text", "text": "done"}]
    assert runtime.kwargs["messages"] == context.messages
    assert runtime.kwargs["messages"] is not context.messages
    assert runtime.kwargs["max_tokens"] == 123
    assert runtime.kwargs["tools"] is agent.PARENT_TOOLS
    assert isinstance(runtime.kwargs["system"], str)


def test_record_llm_response_updates_context_and_logs_usage() -> None:
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    response = FakeResponse([{"type": "text", "text": "done"}])

    with patch("agent.log_usage") as mocked_log_usage:
        agent.record_llm_response(context, response)

    assert context.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    mocked_log_usage.assert_called_once_with("main", response)


def test_should_continue_with_tools() -> None:
    assert agent.should_continue_with_tools(FakeResponse([], stop_reason="tool_use")) is True
    assert agent.should_continue_with_tools(FakeResponse([], stop_reason="end_turn")) is False


def test_step_agent_uses_injected_deps_without_runtime() -> None:
    events = []
    response = FakeResponse([{"type": "text", "text": "done"}], stop_reason="end_turn")
    context = RunContext(
        messages=[{"role": "user", "content": "hello"}],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    deps = AgentDeps(
        compact_context=lambda ctx: events.append("compact"),
        call_llm=lambda ctx: response,
        record_llm_response=lambda ctx, resp: events.append("record_response"),
        should_continue_with_tools=lambda resp: False,
        execute_tool_uses=lambda ctx, resp: ([], False),
        record_tool_results=lambda ctx, results, manual: events.append("record_tools"),
        handle_circuit_open=lambda ctx, error: events.append("circuit"),
    )

    state = AgentState()
    state = step_agent(context, state, deps)
    state = step_agent(context, state, deps)
    state = step_agent(context, state, deps)

    assert events == ["compact", "record_response"]
    assert state.phase == AgentPhase.FINISHED
    assert state.terminal_reason == TerminalReason.END_TURN
    assert state.turn == 1


def test_step_agent_records_tools_and_loops_to_compact() -> None:
    response = FakeResponse([], stop_reason="tool_use")
    tool_results = [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}]
    events = []
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )

    deps = AgentDeps(
        compact_context=lambda ctx: events.append("compact"),
        call_llm=lambda ctx: response,
        record_llm_response=lambda ctx, resp: events.append("record_response"),
        should_continue_with_tools=lambda resp: True,
        execute_tool_uses=lambda ctx, resp: (tool_results, True),
        record_tool_results=lambda ctx, results, manual: events.append(("record_tools", results, manual)),
        handle_circuit_open=lambda ctx, error: events.append("circuit"),
    )

    state = AgentState()
    for _ in range(5):
        state = step_agent(context, state, deps)

    assert events == ["compact", "record_response", ("record_tools", tool_results, True)]
    assert state.phase == AgentPhase.COMPACT_CONTEXT
    assert state.turn == 1
    assert state.response is None
    assert state.tool_results is None
    assert state.manual_compact is False


def test_execute_tool_uses_returns_tool_results() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"workspace"}, deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    response = FakeResponse([FakeToolBlock(name="workspace", block_id="tool-1")], stop_reason="tool_use")

    with patch("message_flow.run_tool", return_value=ToolRun(Result.success("ok"))):
        tool_results, manual_compact = agent.execute_tool_uses(context, response)

    assert manual_compact is False
    assert len(tool_results) == 1
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool_name"] == "workspace"
    assert tool_results[0]["tool_use_id"] == "tool-1"
    assert '"ok": true' in tool_results[0]["content"]
    assert "cache_control" not in tool_results[0]


def test_execute_tool_uses_caches_large_tool_result() -> None:
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"workspace"}, deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    response = FakeResponse([FakeToolBlock(name="workspace", block_id="tool-1")], stop_reason="tool_use")
    large_result = Result.success("x" * 3000)

    with patch("message_flow.run_tool", return_value=ToolRun(large_result)):
        tool_results, _ = agent.execute_tool_uses(context, response)

    assert tool_results[0]["cache_control"] == {"type": "ephemeral"}


def test_record_tool_results_updates_context_and_compacts() -> None:
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    tool_results = [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}]

    with patch.object(context, "force_auto_compact") as mocked_compact:
        agent.record_tool_results(context, tool_results, manual_compact=True)

    assert context.messages == [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "{}"}],
        },
    ]
    mocked_compact.assert_not_called()
    assert context.pending_force_compact_hint == ""


def test_compact_context_for_llm_consumes_pending_force_compact() -> None:
    context = RunContext(
        messages=[],
        policy=empty_policy(),
        approval=ApprovalFlow.require_confirmation(set()),
    )
    context.request_force_compact("keep current bug report")

    with (
        patch.object(context, "force_auto_compact") as mocked_force_compact,
        patch.object(context, "micro_compact") as mocked_micro_compact,
        patch.object(context, "auto_compact_if_needed") as mocked_auto_compact,
    ):
        agent.compact_context_for_llm(context)

    mocked_force_compact.assert_called_once_with(hint="keep current bug report")
    mocked_micro_compact.assert_not_called()
    mocked_auto_compact.assert_not_called()
    assert context.pending_force_compact_hint is None


def run_all() -> None:
    test_resolve_approval_approves_for_session()
    test_agent_loop_updates_run_context_messages()
    test_agent_loop_prepares_context_before_llm_call()
    test_agent_loop_records_message_when_circuit_is_open()
    test_call_llm_uses_run_context_messages()
    test_record_llm_response_updates_context_and_logs_usage()
    test_should_continue_with_tools()
    test_execute_tool_uses_returns_tool_results()
    test_execute_tool_uses_caches_large_tool_result()
    test_record_tool_results_updates_context_and_compacts()
    test_compact_context_for_llm_consumes_pending_force_compact()


if __name__ == "__main__":
    run_all()
    print("ok")
