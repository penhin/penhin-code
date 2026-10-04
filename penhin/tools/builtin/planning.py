"""Validate planning questions and final proposals before host interaction."""
from __future__ import annotations

from typing import TypedDict

from penhin.result import Result
from penhin.tools.types import ToolEffect, ToolOutcome


class PlanningQuestion(TypedDict):
    question: str
    options: list[str]


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                    "options": {"type": "array", "minItems": 3, "maxItems": 3,
                                "items": {"type": "string", "minLength": 1}},
                },
                "required": ["question", "options"], "additionalProperties": False,
            },
        },
        "content": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


def valid_plan_input(questions: object, content: object) -> bool:
    if questions is not None:
        if content is not None or not isinstance(questions, list) or not questions:
            return False
        for item in questions:
            if not isinstance(item, dict) or set(item) != {"question", "options"}:
                return False
            if not isinstance(item["question"], str) or not item["question"].strip():
                return False
            options = item["options"]
            if not isinstance(options, list) or len(options) != 3:
                return False
            if not all(isinstance(option, str) and option.strip() for option in options):
                return False
            if len({" ".join(option.casefold().split()) for option in options}) != 3:
                return False
    return content is None or isinstance(content, str) and bool(content.strip())


def plan_outcome(questions: list[PlanningQuestion] | None = None, content: str | None = None) -> ToolOutcome:
    if not valid_plan_input(questions, content):
        return ToolOutcome(Result.failure(
            "Supply questions with exactly three distinct nonempty options each, OR one nonempty final plan in content. Omit both to begin planning.",
            code="invalid_tool_input",
        ))
    return ToolOutcome(Result.success(), (ToolEffect("present_plan", {"questions": questions, "content": content}),))
