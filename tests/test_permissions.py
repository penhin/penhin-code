import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context import RunContext
from permissions import (
    InvalidTransitionError,
    PermissionMode,
    normalize_permission_mode,
    transition_mode,
)
from tool_runtime import PermissionPolicy


def test_permission_modes_are_runtime_parent_modes_only() -> None:
    assert {mode.value for mode in PermissionMode} == {
        "default",
        "auto-review",
        "full-access",
    }


def test_normalize_permission_mode_rejects_unknown_mode() -> None:
    raised = False
    try:
        normalize_permission_mode("verification")
    except ValueError:
        raised = True

    assert raised is True


def test_transition_between_parent_modes() -> None:
    ctx = RunContext(messages=[], policy=PermissionPolicy(allow=set()), approval=None)

    assert transition_mode(PermissionMode.DEFAULT, PermissionMode.AUTO_REVIEW, ctx) == PermissionMode.AUTO_REVIEW
    assert transition_mode(PermissionMode.AUTO_REVIEW, PermissionMode.FULL_ACCESS, ctx) == PermissionMode.FULL_ACCESS
    assert transition_mode(PermissionMode.FULL_ACCESS, PermissionMode.DEFAULT, ctx) == PermissionMode.DEFAULT


def test_transition_same_mode_is_noop() -> None:
    ctx = RunContext(messages=[], policy=PermissionPolicy(allow=set()), approval=None)

    result = transition_mode(PermissionMode.DEFAULT, PermissionMode.DEFAULT, ctx)

    assert result == PermissionMode.DEFAULT


def test_transition_rejects_missing_matrix_entry() -> None:
    class UnknownMode:
        value = "verification"

    raised = False
    try:
        transition_mode(PermissionMode.DEFAULT, UnknownMode())  # type: ignore[arg-type]
    except InvalidTransitionError:
        raised = True

    assert raised is True
