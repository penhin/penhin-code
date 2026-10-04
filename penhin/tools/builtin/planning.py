"""The plan tool validates proposals and declares a host-owned selection effect."""
from __future__ import annotations

from penhin.result import Result
from penhin.tools.types import ToolEffect, ToolOutcome


PLAN_FIELDS = ("title", "scope", "risks", "cost", "verification")
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "alternatives": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {name: {"type": "string", "minLength": 1} for name in PLAN_FIELDS},
                "required": list(PLAN_FIELDS), "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def plan_outcome(alternatives: list[dict[str, str]] | None = None) -> ToolOutcome:
    if alternatives is not None:
        valid = (
            isinstance(alternatives, list) and len(alternatives) == 3
            and all(isinstance(option, dict) and set(option) == set(PLAN_FIELDS)
                    and all(isinstance(value, str) and value.strip() for value in option.values())
                    for option in alternatives)
        )
        if not valid:
            return ToolOutcome(Result.failure(
                "Provide exactly three alternatives, each with nonempty title, scope, risks, cost, and verification.",
                code="invalid_tool_input",
            ))
        proposals = {
            tuple(" ".join(option[name].casefold().split()) for name in PLAN_FIELDS[1:])
            for option in alternatives
        }
        if len(proposals) != 3:
            return ToolOutcome(Result.failure("Provide three distinct alternatives, not duplicate proposals.", code="invalid_tool_input"))
    return ToolOutcome(Result.success(), (ToolEffect("present_plan", {"alternatives": alternatives}),))
