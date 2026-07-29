"""
Tests for plan mode tools (enter_plan / exit_plan).

Tests exercise the real `execute_tool` path used by the agent loop,
mocking only config reads/writes that touch ~/.penhin/config.json.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context import RunContext
from permissions import PermissionMode
from tool_runtime import (
    ApprovalFlow,
    PermissionPolicy,
    execute_tool,
)
from tools.plans import PLANS_DIR


def _context(pre_plan_mode: PermissionMode | None = None) -> RunContext:
    ctx = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"enter_plan", "exit_plan"}, deny=set()),
        approval=ApprovalFlow.preapproved({"enter_plan", "exit_plan"}),
    )
    ctx.pre_plan_mode = pre_plan_mode
    return ctx


def _cleanup_plan_files() -> None:
    """Remove any plan .md files left on disk after a test."""
    for p in PLANS_DIR.glob("*.md"):
        p.unlink()


# ── enter_plan ──────────────────────────────────────────────────────────


def test_enter_plan_requires_context() -> None:
    result = execute_tool("enter_plan", {})
    assert result.result.ok is False
    assert result.result.meta["code"] == "no_context"


@patch("tools.plan_mode.get_permission_mode", return_value="default")
@patch("tools.plan_mode.set_permission_mode")
def test_enter_plan_switches_to_plan(
    mock_set: object, mock_get: object  # noqa: ARG001
) -> None:
    ctx = _context()
    result = execute_tool("enter_plan", {}, context=ctx)

    assert result.result.ok is True
    msg = result.result.message
    assert "Entered plan mode" in msg
    assert "READ-ONLY" in msg
    assert ctx.pre_plan_mode == PermissionMode.DEFAULT
    mock_set.assert_called_once_with("plan")  # type: ignore[attr-defined]


@patch("tools.plan_mode.get_permission_mode", return_value="plan")
@patch("tools.plan_mode.set_permission_mode")
def test_enter_plan_already_in_plan(
    mock_set: object, mock_get: object  # noqa: ARG001
) -> None:
    ctx = _context()
    result = execute_tool("enter_plan", {}, context=ctx)

    assert result.result.ok is True
    assert "Already in plan mode" in result.result.message
    mock_set.assert_not_called()  # type: ignore[attr-defined]


@patch("tools.plan_mode.get_permission_mode", return_value="unknown")
def test_enter_plan_unknown_mode(mock_get: object) -> None:  # noqa: ARG001
    ctx = _context()
    result = execute_tool("enter_plan", {}, context=ctx)

    # run_enter_plan returns an informational string for unknown modes
    assert result.result.ok is True
    assert "Unknown current mode" in result.result.message


def test_enter_plan_keeps_pre_plan_across_noop() -> None:
    """Entering plan from default saves pre_plan_mode only once."""
    ctx = _context(pre_plan_mode=PermissionMode.DEFAULT)

    with (
        patch("tools.plan_mode.get_permission_mode", return_value="plan"),
        patch("tools.plan_mode.set_permission_mode"),
    ):
        result = execute_tool("enter_plan", {}, context=ctx)

    # Already in plan mode — pre_plan_mode unchanged
    assert result.result.ok is True
    assert "Already in plan mode" in result.result.message
    assert ctx.pre_plan_mode == PermissionMode.DEFAULT


# ── exit_plan ───────────────────────────────────────────────────────────


@patch("tools.plan_mode.get_permission_mode", return_value="plan")
@patch("tools.plan_mode.set_permission_mode")
def test_exit_plan_restores_mode_and_saves_plan(
    mock_set: object, mock_get: object  # noqa: ARG001
) -> None:
    ctx = _context(pre_plan_mode=PermissionMode.DEFAULT)
    plan = "1. refactor module\n2. update tests\n3. verify"

    result = execute_tool("exit_plan", {"plan_content": plan}, context=ctx)

    assert result.result.ok is True
    msg = result.result.message
    assert "Plan saved" in msg
    assert "plan_slug:" in msg
    assert "1. refactor module" in msg
    assert ctx.pre_plan_mode is None
    mock_set.assert_called_once_with("default")  # type: ignore[attr-defined]

    _cleanup_plan_files()


@patch("tools.plan_mode.get_permission_mode", return_value="default")
@patch("tools.plan_mode.set_permission_mode")
def test_exit_plan_not_in_plan_mode(
    mock_set: object, mock_get: object  # noqa: ARG001
) -> None:
    ctx = _context()
    result = execute_tool("exit_plan", {"plan_content": "irrelevant"}, context=ctx)

    assert result.result.ok is True
    assert "Not in plan mode" in result.result.message
    mock_set.assert_not_called()  # type: ignore[attr-defined]


def test_exit_plan_missing_plan_content() -> None:
    ctx = _context(pre_plan_mode=PermissionMode.DEFAULT)
    with patch("tools.plan_mode.get_permission_mode", return_value="plan"):
        result = execute_tool("exit_plan", {}, context=ctx)

    assert result.result.ok is False
    assert result.result.meta.get("code") == "invalid_tool_input"
    assert "Missing required input" in result.result.error


def test_exit_plan_requires_context() -> None:
    result = execute_tool("exit_plan", {"plan_content": "test"})
    assert result.result.ok is False
    assert result.result.meta["code"] == "no_context"


# ── Runner ──────────────────────────────────────────────────────────────
