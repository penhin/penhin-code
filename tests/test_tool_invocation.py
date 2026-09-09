from penhin.agent.context import RunContext
from penhin.result import Result
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.tools.execution.invocation import ToolInvocation
from penhin.tools.types import ToolEffect, ToolOutcome


def test_invocation_applies_registered_effect_and_records_it() -> None:
    context = RunContext([], PermissionPolicy(allow={"compact"}), ApprovalFlow.preapproved({"compact"}))
    invocation = ToolInvocation()

    run = invocation.invoke("compact", {}, context.policy, context.approval, context)

    assert run.result.ok
    assert run.manual_compact is True
    assert run.effects == [{"kind": "compact_context", "status": "applied"}]


def test_invocation_rejects_unknown_effect_without_mutating_session() -> None:
    context = RunContext([], PermissionPolicy(allow={"demo"}), ApprovalFlow.preapproved({"demo"}))
    invocation = ToolInvocation(effect_handlers={})
    outcome = ToolOutcome(Result.success("done"), (ToolEffect("unknown", {}),))

    run = invocation.apply_outcome("demo", outcome, context)

    assert not run.result.ok
    assert run.result.meta["code"] == "unknown_tool_effect"
    assert run.effects == [{"kind": "unknown", "status": "failed", "code": "unknown_tool_effect"}]
    assert context.pending_force_compact_hint is None


def test_effect_failure_stops_later_effects() -> None:
    context = RunContext([], PermissionPolicy(allow={"demo"}), ApprovalFlow.preapproved({"demo"}))
    invoked = []
    invocation = ToolInvocation(effect_handlers={
        "fail": lambda _payload, _context: Result.failure("no", code="effect_failed"),
        "later": lambda _payload, _context: invoked.append("later") or Result.success(),
    })

    run = invocation.apply_outcome("demo", ToolOutcome(Result.success(), (ToolEffect("fail", {}), ToolEffect("later", {}))), context)

    assert not run.result.ok
    assert invoked == []
    assert run.effects == [{"kind": "fail", "status": "failed", "code": "effect_failed"}]


def test_invocation_rejects_invalid_effect_payload_before_executor_mutates() -> None:
    context = RunContext([], PermissionPolicy(allow={"demo"}), ApprovalFlow.preapproved({"demo"}))
    mutated = []
    invocation = ToolInvocation(effect_handlers={
        "demo": ({"required": {"value": str}, "optional": set()}, lambda _payload, _context: mutated.append(True) or Result.success()),
    })

    run = invocation.apply_outcome("demo", ToolOutcome(Result.success(), (ToolEffect("demo", {"unexpected": True}),)), context)

    assert not run.result.ok
    assert run.result.meta["code"] == "invalid_tool_effect"
    assert mutated == []
