"""Session-owned planning decisions, independent of permission mode."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from penhin.result import Result
from penhin.tools.builtin.planning import PLAN_FIELDS

if TYPE_CHECKING:
    from penhin.agent.context import RunContext


@dataclass
class PlanningState:
    active: bool = False
    alternatives: list[dict[str, str]] = field(default_factory=list)
    selected: dict[str, str] | None = None


def present_plan(context: RunContext | None, alternatives: list[dict[str, str]] | None) -> Result:
    if context is None:
        return Result.failure("No active session for planning.", code="no_context")
    from penhin.cli import ui

    save_planning(context, PlanningState(active=True, alternatives=alternatives or []))
    if alternatives is None:
        return Result.success("Planning started. Inspect with read, then call plan with exactly three material alternatives. Implementation waits for user selection.")
    labels = [option["title"] + "\n" + "\n".join(
        f"    {name.capitalize()}: {option[name]}" for name in PLAN_FIELDS[1:]
    ) + "\n" for option in alternatives]
    options = tuple((str(index), label) for index, label in enumerate(labels, 1))
    try:
        choice = ui.prompt_select("Select a plan, or suggest a different direction", options + (("custom", "Suggest another direction"),), group_by_prefix=False)
        if choice == "custom":
            suggestion = ui.prompt_text("Your planning suggestion")
            if suggestion.strip():
                save_planning(context, PlanningState(active=True))
                return Result.success("Revise the plan into exactly three alternatives using the user's suggestion. Do not implement yet.", data={"suggestion": suggestion})
        elif choice in {"1", "2", "3"}:
            return select_plan(context, int(choice))
    except (EOFError, KeyboardInterrupt, ValueError):
        pass
    return Result.success("Waiting for plan selection. Reply 1, 2, or 3, or provide a planning suggestion.", data={"awaiting_selection": True})


def select_plan(context: RunContext, number: int) -> Result:
    save_planning(context, PlanningState(selected=dict(context.planning.alternatives[number - 1])))
    return Result.success("Plan selected. Continue implementation now under the existing permission policy.", data={"selected": context.planning.selected})


def save_planning(context: RunContext, state: PlanningState) -> None:
    if context.session_manager is not None:
        context.session_manager.append_entry(
            "planning", active=state.active,
            alternatives=state.alternatives, selected=state.selected,
        )
    context.planning = state
