"""Governed Tool Invocation and its declared Tool Effects."""

from __future__ import annotations

from collections.abc import Callable

from penhin.result import Result
from penhin.tools.types import ToolEffect, ToolOutcome

from .service import ToolRun, run_tool


EffectHandler = Callable[[dict, object], Result]


def _compact(_payload: dict, context: object) -> Result:
    if context is None:
        return Result.success()
    context.request_force_compact()
    return Result.success()


def _snip(payload: dict, context: object) -> Result:
    if context is None:
        return Result.failure("No active session to snip.", code="missing_context")
    selectors_input = payload.get("selectors")
    selector_texts = selectors_input.split() if isinstance(selectors_input, str) else [str(item) for item in selectors_input] if isinstance(selectors_input, list) else None
    if selector_texts is None:
        return Result.failure("Invalid input: selectors must be an array", code="invalid_tool_input")
    try:
        from penhin.agent.context import parse_snip_selectors
        selectors = parse_snip_selectors(selector_texts)
    except ValueError:
        return Result.failure("Invalid snip selector. Use turn numbers or ranges like 2 or 2-4.", code="invalid_tool_input")
    snipped = context.force_snip_turns(selectors)
    return Result.success(f"Marked {snipped} messages as snipped.", snipped=snipped)


def _enter_plan(_payload: dict, context: object) -> Result:
    from penhin.tools.builtin.plan_mode import run_enter_plan
    return run_enter_plan(context)


def _save_plan_and_exit(payload: dict, context: object) -> Result:
    from penhin.tools.builtin.plan_mode import run_exit_plan
    return run_exit_plan(payload.get("plan_content"), context)


class ToolInvocation:
    """The sole module that validates, applies, and observes Tool Effects."""

    def __init__(self, effect_handlers: dict[str, EffectHandler] | None = None) -> None:
        self._effect_handlers = effect_handlers if effect_handlers is not None else {
            "compact_context": _compact,
            "snip_turns": _snip,
            "enter_plan_mode": _enter_plan,
            "save_plan_and_exit": _save_plan_and_exit,
        }

    def invoke(self, tool_name, tool_input, policy, approval, context=None, catalog=None) -> ToolRun:
        kwargs = {"context": context}
        if catalog is not None:
            kwargs["catalog"] = catalog
        tool_run = run_tool(tool_name, tool_input, policy, approval, **kwargs)
        return tool_run

    def apply_outcome(self, tool_name: str, outcome: ToolOutcome, context: object) -> ToolRun:
        if not outcome.result.ok:
            return ToolRun(outcome.result)
        effects: list[dict[str, str]] = []
        manual_compact = False
        for effect in outcome.effects:
            handler = self._effect_handlers.get(effect.kind)
            if handler is None:
                effects.append({"kind": effect.kind, "status": "failed", "code": "unknown_tool_effect"})
                return ToolRun(Result.failure(f"Unknown Tool Effect: {effect.kind}", code="unknown_tool_effect"), manual_compact=manual_compact, effects=effects)
            result = handler(effect.payload, context)
            if not result.ok:
                effects.append({"kind": effect.kind, "status": "failed", "code": result.meta.get("code", "tool_effect_failed")})
                return ToolRun(result, manual_compact=manual_compact, effects=effects)
            if result.message:
                outcome.result.message = result.message
            if result.data is not None:
                outcome.result.data = result.data
            outcome.result.meta.update(result.meta)
            effects.append({"kind": effect.kind, "status": "applied"})
            manual_compact = manual_compact or effect.kind == "compact_context"
        return ToolRun(outcome.result, manual_compact=manual_compact, effects=effects)
