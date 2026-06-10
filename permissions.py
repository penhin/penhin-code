from typing import Literal

from tool_runtime import (
    ApprovalFlow,
    PARENT_AGENT_POLICY,
    PermissionPolicy,
    tool_names_by_category,
)
from tools.registry import TOOL_SPECS
from tools.types import ToolCategory


PermissionMode = Literal["default", "auto-review", "full-access"]
PERMISSION_MODES = {"default", "auto-review", "full-access"}


def normalize_permission_mode(mode: str) -> PermissionMode:
    normalized = mode.strip().lower()
    if normalized not in PERMISSION_MODES:
        raise ValueError(f"Unknown permission mode: {mode}")
    return normalized


def policy_for_mode(mode: str) -> PermissionPolicy:
    mode = normalize_permission_mode(mode)
    if mode == "auto-review":
        allow = tool_names_by_category({ToolCategory.readonly, ToolCategory.state})
        if "compact" in TOOL_SPECS:
            allow.add("compact")
        return PermissionPolicy(
            allow=allow,
            deny={"write", "edit", "bash", "task", "background_start"},
        )
    return PARENT_AGENT_POLICY


def approval_for_mode(mode: str, policy: PermissionPolicy) -> ApprovalFlow:
    mode = normalize_permission_mode(mode)
    if mode in {"auto-review", "full-access"}:
        return ApprovalFlow.preapproved(policy.allow)
    return ApprovalFlow.require_confirmation(policy.allow)


def permission_setup(mode: str) -> tuple[PermissionPolicy, ApprovalFlow]:
    policy = policy_for_mode(mode)
    return policy, approval_for_mode(mode, policy)
