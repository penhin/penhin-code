"""The host-owned planning response protocol; never a model tool."""
from __future__ import annotations

import json
from typing import TypedDict


class PlanningQuestion(TypedDict):
    question: str
    options: list[str]


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


PROTOCOL = (
    'Planning mode was explicitly enabled by the user through the CLI. '
    'Use read for inspection. For each non-tool response, output ONLY one JSON object '
    '(no Markdown fences or surrounding prose): '
    '{"questions":[{"question":"...","options":["...","...","..."]}]} '
    'to clarify requirements, OR {"content":"complete Markdown plan"} for final review. '
    'Each question needs exactly three distinct options; the host adds Other. '
    'You may ask multiple groups, or further questions after answers. '
    'The final plan must cover scope, approach, risks, cost, and verification. '
    'Answers and feedback never approve implementation. Only the host records final user approval. '
    'Never output approval or invoke a planning tool. After feedback, revise and submit a new final plan.'
)


def parse_response(blocks: list[dict]) -> tuple[list[PlanningQuestion] | None, str | None]:
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) not in ({"questions"}, {"content"}):
        raise ValueError("Expected questions or content")
    questions, content = payload.get("questions"), payload.get("content")
    if not valid_plan_input(questions, content) or questions is None and content is None:
        raise ValueError("Invalid questions or content")
    return questions, content
