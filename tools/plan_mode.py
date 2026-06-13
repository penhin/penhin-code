from __future__ import annotations

from typing import TYPE_CHECKING

from config import get_permission_mode, set_permission_mode
from permissions import PermissionMode, permission_setup, transition_mode

if TYPE_CHECKING:
    from context import RunContext

from .plans import write_plan


def run_enter_plan(context: RunContext) -> str:
    current = get_permission_mode()
    if current == PermissionMode.PLAN.value:
        return "Already in plan mode."
    if current not in {m.value for m in PermissionMode}:
        return f"Unknown current mode: {current}"

    target = PermissionMode.PLAN
    transition_mode(PermissionMode(current), target, context)
    set_permission_mode(target.value)

    new_policy, new_approval = permission_setup(target.value)
    context.policy = new_policy
    context.approval = new_approval

    return (
        "Entered plan mode. You are now in READ-ONLY mode.\n"
        "You can explore the codebase using read, list, search, glob, task_show, "
        "and other read-only tools.\n"
        "You CANNOT create, edit, or delete any files.\n"
        "Design a complete step-by-step implementation plan.\n"
        "When ready, call exit_plan with your full plan to restore write access."
    )


def run_exit_plan(context: RunContext, plan_content: str) -> str:
    current = get_permission_mode()
    if current != PermissionMode.PLAN.value:
        return "Not in plan mode. Call enter_plan first."

    plan_path = write_plan(plan_content)
    plan_slug = plan_path.stem

    pre_plan = context.pre_plan_mode or PermissionMode.DEFAULT.value
    transition_mode(PermissionMode.PLAN, PermissionMode(pre_plan), context)
    set_permission_mode(pre_plan)

    new_policy, new_approval = permission_setup(pre_plan)
    context.policy = new_policy
    context.approval = new_approval

    return (
        f"Plan saved to {plan_path}\n\n"
        f"plan_slug: {plan_slug}\n\n"
        "=== PLAN ===\n"
        f"{plan_content}\n"
        "============\n\n"
        "Plan mode exited. Now call task_start with plan_slug and 2-5 execution steps "
        "derived from the plan above, then execute each step."
    )
