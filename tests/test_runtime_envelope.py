from pathlib import Path

import pytest

from penhin.agent.context import RunContext
from penhin.agent.session_manager import SessionManager
from penhin.runtime.envelope import RuntimeBudget, RuntimeEnvelope
from penhin.evaluation.observer import EvaluationObserver, observing, read_events
from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.tools.registry import DEFAULT_TOOL_CATALOG


class Runtime:
    provider_id = "openai"
    model = "gpt-test"
    max_tokens = 1_000


def envelope() -> RuntimeEnvelope:
    return RuntimeEnvelope.root(
        Runtime(),
        PermissionPolicy(allow={"read", "write"}),
        DEFAULT_TOOL_CATALOG,
        cwd=Path("/tmp/workspace"),
    )


def test_root_envelope_receipt_exposes_resolved_capabilities() -> None:
    receipt = envelope().receipt()

    assert receipt["provider"] == "openai"
    assert receipt["model"] == "gpt-test"
    assert receipt["sandbox"] is False
    assert receipt["writable_roots"] == ["/tmp/workspace"]
    assert set(receipt["tools"]) == {"read", "write"}
    assert receipt["budget"] == {"max_tokens": 1_000, "max_turns": None, "max_tool_calls": None}


def test_child_envelope_can_only_narrow_parent_capabilities() -> None:
    parent = envelope()
    child = parent.narrow(tools={"read"}, credential_capabilities=set(), budget=RuntimeBudget(max_tokens=200))

    assert child.lifecycle == "child-run"
    assert child.tools == {"read"}
    assert child.credential_capabilities == set()
    assert child.budget.max_tokens == 200

    with pytest.raises(ValueError, match="tools"):
        child.narrow(tools={"read", "bash"})
    with pytest.raises(ValueError, match="max_tokens"):
        child.narrow(budget=RuntimeBudget(max_tokens=500))


def test_widening_rejection_is_observable_from_the_envelope_boundary(tmp_path: Path) -> None:
    with observing(EvaluationObserver(tmp_path, "run")):
        with pytest.raises(ValueError, match="tools"):
            envelope().narrow(tools={"read", "bash"})

    events = read_events(tmp_path)
    assert events[-1]["event_type"] == "runtime_envelope_widening_rejected"
    assert "tools" in events[-1]["payload"]["reason"]


def test_context_records_envelope_as_append_only_session_evidence(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"read"}),
        approval=ApprovalFlow.require_confirmation({"read"}),
        session_manager=manager,
        runtime_envelope=envelope(),
    )

    context.record_runtime_envelope()
    context.record_runtime_envelope()

    entries = [entry for entry in manager.entries if entry["type"] == "runtime_envelope"]
    assert len(entries) == 1
    assert entries[0]["envelope"]["version"] == 1
    assert entries[0]["envelope"]["model"] == "gpt-test"
