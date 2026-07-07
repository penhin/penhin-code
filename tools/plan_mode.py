from __future__ import annotations

from typing import TYPE_CHECKING

from config import get_permission_mode, set_permission_mode
from permissions import PermissionMode
from result import Result
from tools.plans import write_plan

if TYPE_CHECKING:
    from context import RunContext


def _mode_from_config(value: str) -> PermissionMode | None:
    try:
        return PermissionMode(value)
    except ValueError:
        return None


def run_enter_plan(context: RunContext | None = None) -> Result:
    if context is None:
        return Result.failure("No active session to enter plan mode.", code="no_context")

    current_mode = get_permission_mode()
    if current_mode == "plan":
        return Result.success("Already in plan mode. Continue planning in READ-ONLY mode.")

    pre_plan_mode = _mode_from_config(current_mode)
    if pre_plan_mode is None:
        return Result.success(
            f"Unknown current mode: {current_mode}. Plan mode was not changed."
        )

    if context.pre_plan_mode is None:
        context.pre_plan_mode = pre_plan_mode

    set_permission_mode("plan")
    return Result.success(
        "Entered plan mode. You are now in READ-ONLY planning mode; prepare the plan, then call exit_plan."
    )


def run_exit_plan(plan_content: str | None = None, context: RunContext | None = None) -> Result:
    if context is None:
        return Result.failure("No active session to exit plan mode.", code="no_context")

    current_mode = get_permission_mode()
    if current_mode != "plan":
        return Result.success("Not in plan mode. No plan was saved.")

    if not plan_content:
        return Result.failure(
            "Missing required input: plan_content",
            code="invalid_tool_input",
            field="plan_content",
        )

    path = write_plan(plan_content)
    plan_slug = path.stem

    restore_mode = context.pre_plan_mode or PermissionMode.DEFAULT
    if isinstance(restore_mode, PermissionMode):
        restore_value = restore_mode.value
    else:
        restore_value = str(restore_mode)

    set_permission_mode(restore_value)
    context.pre_plan_mode = None

    return Result.success(
        f"Plan saved.\nplan_slug: {plan_slug}\n\n{plan_content}",
        plan_slug=plan_slug,
        path=str(path),
    )
