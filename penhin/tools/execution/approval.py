from __future__ import annotations

from dataclasses import dataclass, field

from penhin.approval_rules import approval_rule_key, bash_prefix_matches
from penhin.tools.registry import TOOL_SPECS
from penhin.tools.types import ToolCategory, ToolInput


def approval_required_tools(tool_names: set[str]) -> set[str]:
    return {
        name for name in tool_names
        if name in TOOL_SPECS and TOOL_SPECS[name].approval.requires_approval
    }


def approval_key(tool_name: str, tool_input: ToolInput) -> str:
    spec = TOOL_SPECS.get(tool_name)
    return tool_name if spec is None else spec.approval.approval_key(tool_name, tool_input)


@dataclass
class PermissionPolicy:
    allow: set[str]
    deny: set[str] = field(default_factory=set)


@dataclass
class ApprovalFlow:
    approved: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=set)
    rejected: set[str] = field(default_factory=set)
    approved_rules: set[str] = field(default_factory=set)

    @classmethod
    def preapproved(cls, tool_names: set[str]) -> ApprovalFlow:
        required = approval_required_tools(tool_names)
        return cls(approved=required, required=required)

    @classmethod
    def require_confirmation(cls, tool_names: set[str]) -> ApprovalFlow:
        return cls(required=approval_required_tools(tool_names))

    def copy(self) -> ApprovalFlow:
        return ApprovalFlow(set(self.approved), set(self.required), set(self.rejected), set(self.approved_rules))

    def approve(self, tool_name: str, tool_input: ToolInput) -> None:
        self.approved.add(approval_key(tool_name, tool_input))

    def reject(self, tool_name: str, tool_input: ToolInput) -> None:
        self.rejected.add(approval_key(tool_name, tool_input))

    def approve_rule(self, tool_name: str, rule: str) -> None:
        self.approved_rules.add(approval_rule_key(tool_name, rule))

    def is_approved(self, tool_name: str, tool_input: ToolInput) -> bool:
        if tool_name in self.approved or approval_key(tool_name, tool_input) in self.approved:
            return True
        if tool_name == "bash":
            command = str(tool_input.get("command", ""))
            return any(
                key.startswith("bash:") and bash_prefix_matches(command, key.removeprefix("bash:"))
                for key in self.approved_rules
            )
        return False

    def is_rejected(self, tool_name: str, tool_input: ToolInput) -> bool:
        return tool_name in self.rejected or approval_key(tool_name, tool_input) in self.rejected


def tool_names_for(scope: str) -> set[str]:
    if scope == "parent":
        return {name for name, spec in TOOL_SPECS.items() if spec.available_to_parent}
    if scope == "child":
        return {name for name, spec in TOOL_SPECS.items() if spec.available_to_child}
    raise ValueError(f"Unknown tool scope: {scope}")


def tool_names_by_category(categories: set[ToolCategory]) -> set[str]:
    return {name for name, spec in TOOL_SPECS.items() if spec.category in categories}


PARENT_AGENT_POLICY = PermissionPolicy(allow=tool_names_for("parent"))


def policy_for_runtime_mode(mode: str) -> PermissionPolicy:
    if mode == "auto-review":
        allow = tool_names_by_category({ToolCategory.readonly, ToolCategory.state})
        if "compact" in TOOL_SPECS:
            allow.add("compact")
        return PermissionPolicy(allow=allow, deny={"write", "edit", "bash", "task", "agent_job_start"})
    return PARENT_AGENT_POLICY


def approval_for_runtime_mode(mode: str, policy: PermissionPolicy) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow) if mode in {"auto-review", "full-access"} else ApprovalFlow.require_confirmation(policy.allow)


def runtime_permission_setup(mode: str) -> tuple[PermissionPolicy, ApprovalFlow]:
    policy = policy_for_runtime_mode(mode)
    return policy, approval_for_runtime_mode(mode, policy)


def default_approval_flow(policy: PermissionPolicy) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow)


__all__ = ["ApprovalFlow", "PermissionPolicy", "approval_key", "default_approval_flow", "runtime_permission_setup", "tool_names_by_category", "tool_names_for"]
