from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from tool_runtime import (
    ApprovalFlow,
    PARENT_AGENT_POLICY,
    PermissionPolicy,
    tool_names_by_category,
)
from tools.registry import TOOL_SPECS
from tools.types import ToolCategory

if TYPE_CHECKING:
    from context import RunContext


class PermissionMode(str, Enum):
    DEFAULT = "default"
    AUTO_REVIEW = "auto-review"
    FULL_ACCESS = "full-access"
    PLAN = "plan"
    VERIFICATION = "verification"


PERMISSION_MODES = {m.value for m in PermissionMode}


VALID_TRANSITIONS: dict[PermissionMode, set[PermissionMode]] = {
    PermissionMode.DEFAULT: {
        PermissionMode.PLAN,
        PermissionMode.AUTO_REVIEW,
        PermissionMode.FULL_ACCESS,
        PermissionMode.VERIFICATION,
    },
    PermissionMode.PLAN: {
        PermissionMode.DEFAULT,
        PermissionMode.AUTO_REVIEW,
        PermissionMode.FULL_ACCESS,
    },
    PermissionMode.AUTO_REVIEW: {
        PermissionMode.DEFAULT,
        PermissionMode.FULL_ACCESS,
        PermissionMode.PLAN,
        PermissionMode.VERIFICATION,
    },
    PermissionMode.FULL_ACCESS: {
        PermissionMode.DEFAULT,
        PermissionMode.AUTO_REVIEW,
        PermissionMode.PLAN,
        PermissionMode.VERIFICATION,
    },
    PermissionMode.VERIFICATION: {
        PermissionMode.DEFAULT,
        PermissionMode.AUTO_REVIEW,
        PermissionMode.FULL_ACCESS,
    },
}


class InvalidTransitionError(ValueError):
    pass


def normalize_permission_mode(mode: str) -> PermissionMode:
    normalized = mode.strip().lower()
    try:
        return PermissionMode(normalized)
    except ValueError:
        raise ValueError(f"Unknown permission mode: {mode}")


def transition_mode(
    current: PermissionMode,
    target: PermissionMode,
    context: RunContext | None = None,
) -> PermissionMode:
    if target == current:
        return current

    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from '{current.value}' to '{target.value}'"
        )

    if target == PermissionMode.PLAN and context is not None:
        context.pre_plan_mode = current

    if current == PermissionMode.PLAN and target != PermissionMode.PLAN and context is not None:
        context.pre_plan_mode = None

    return target


def is_readonly_mode(mode: PermissionMode) -> bool:
    return mode == PermissionMode.PLAN


def verification_tool_allowlist() -> set[str]:
    allow = {
        "bash",
        "compact",
        "glob",
        "list",
        "read",
        "search",
        "task_show",
        "todo_show",
        "workspace",
    }
    return {name for name in allow if name in TOOL_SPECS}


def policy_for_mode(mode: str) -> PermissionPolicy:
    pm = normalize_permission_mode(mode)

    if pm in (PermissionMode.PLAN, PermissionMode.AUTO_REVIEW):
        allow = tool_names_by_category({ToolCategory.readonly, ToolCategory.state})
        if "compact" in TOOL_SPECS:
            allow.add("compact")
        return PermissionPolicy(
            allow=allow,
            deny={"write", "edit", "bash", "task", "background_start"},
        )

    if pm == PermissionMode.VERIFICATION:
        return PermissionPolicy(
            allow=verification_tool_allowlist(),
            deny={
                "background_start",
                "edit",
                "task",
                "task_complete",
                "task_start",
                "todo_clear",
                "todo_done",
                "todo_set",
                "write",
            },
        )

    return PARENT_AGENT_POLICY


def approval_for_mode(mode: str, policy: PermissionPolicy) -> ApprovalFlow:
    pm = normalize_permission_mode(mode)

    if pm in (
        PermissionMode.AUTO_REVIEW,
        PermissionMode.FULL_ACCESS,
        PermissionMode.PLAN,
        PermissionMode.VERIFICATION,
    ):
        return ApprovalFlow.preapproved(policy.allow)

    return ApprovalFlow.require_confirmation(policy.allow)


def permission_setup(mode: str) -> tuple[PermissionPolicy, ApprovalFlow]:
    policy = policy_for_mode(mode)
    return policy, approval_for_mode(mode, policy)
