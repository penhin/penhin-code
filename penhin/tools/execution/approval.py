from __future__ import annotations

from dataclasses import dataclass, field

from penhin.approval_rules import approval_rule_key, bash_prefix_matches
from penhin.tools.catalog import ToolCatalog
from penhin.tools.registry import DEFAULT_TOOL_CATALOG
from penhin.tools.types import ToolCategory, ToolInput


def approval_required_tools(tool_names: set[str], catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> set[str]:
    return {
        name for name in tool_names
        if (spec := catalog.get(name)) is not None and spec.approval.requires_approval
    }


def approval_key(tool_name: str, tool_input: ToolInput, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> str:
    spec = catalog.get(tool_name)
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
    def preapproved(cls, tool_names: set[str], catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ApprovalFlow:
        required = approval_required_tools(tool_names, catalog)
        return cls(approved=required, required=required)

    @classmethod
    def require_confirmation(cls, tool_names: set[str], catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ApprovalFlow:
        return cls(required=approval_required_tools(tool_names, catalog))

    def copy(self) -> ApprovalFlow:
        return ApprovalFlow(set(self.approved), set(self.required), set(self.rejected), set(self.approved_rules))

    def approve(self, tool_name: str, tool_input: ToolInput, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> None:
        self.approved.add(approval_key(tool_name, tool_input, catalog))

    def reject(self, tool_name: str, tool_input: ToolInput, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> None:
        self.rejected.add(approval_key(tool_name, tool_input, catalog))

    def approve_rule(self, tool_name: str, rule: str) -> None:
        self.approved_rules.add(approval_rule_key(tool_name, rule))

    def is_approved(self, tool_name: str, tool_input: ToolInput, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> bool:
        if tool_name in self.approved or approval_key(tool_name, tool_input, catalog) in self.approved:
            return True
        if tool_name == "bash":
            command = str(tool_input.get("command", ""))
            return any(
                key.startswith("bash:") and bash_prefix_matches(command, key.removeprefix("bash:"))
                for key in self.approved_rules
            )
        return False

    def is_rejected(self, tool_name: str, tool_input: ToolInput, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> bool:
        return tool_name in self.rejected or approval_key(tool_name, tool_input, catalog) in self.rejected


def tool_names_for(scope: str, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> set[str]:
    return catalog.names(scope)


def tool_names_by_category(
    categories: set[ToolCategory],
    catalog: ToolCatalog = DEFAULT_TOOL_CATALOG,
) -> set[str]:
    return {name for name, spec in catalog.specs.items() if spec.category in categories}


PARENT_AGENT_POLICY = PermissionPolicy(allow=tool_names_for("parent"))


def policy_for_runtime_mode(mode: str, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> PermissionPolicy:
    if mode == "auto-review":
        allow = tool_names_by_category({ToolCategory.readonly, ToolCategory.state}, catalog)
        if catalog.get("compact") is not None:
            allow.add("compact")
        return PermissionPolicy(allow=allow, deny={"write", "edit", "bash", "task", "agent_job_start"})
    return PermissionPolicy(allow=tool_names_for("parent", catalog))


def approval_for_runtime_mode(mode: str, policy: PermissionPolicy, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow, catalog) if mode in {"auto-review", "full-access"} else ApprovalFlow.require_confirmation(policy.allow, catalog)


def runtime_permission_setup(mode: str, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> tuple[PermissionPolicy, ApprovalFlow]:
    policy = policy_for_runtime_mode(mode, catalog)
    return policy, approval_for_runtime_mode(mode, policy, catalog)


def default_approval_flow(policy: PermissionPolicy, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ApprovalFlow:
    return ApprovalFlow.preapproved(policy.allow, catalog)


__all__ = ["ApprovalFlow", "PermissionPolicy", "approval_key", "default_approval_flow", "runtime_permission_setup", "tool_names_by_category", "tool_names_for"]
