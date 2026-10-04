"""Persisted planning dialogue, independent of the tool permission policy."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from penhin.result import Result
from penhin.agent.planning_protocol import PlanningQuestion, parse_response

if TYPE_CHECKING:
    from penhin.agent.context import RunContext


@dataclass
class PlanningState:
    active: bool = False
    questions: list[PlanningQuestion] = field(default_factory=list)
    answers: list[dict[str, str]] = field(default_factory=list)
    content: str = ""
    feedback: list[str] = field(default_factory=list)
    approved: bool = False
    awaiting: Literal["", "question", "answer", "approval", "feedback"] = ""

    @classmethod
    def from_entry(cls, entry: dict) -> PlanningState:
        # Retired three-alternative checkpoints require a fresh final approval.
        return cls(**entry["state"]) if "state" in entry else cls(active=True)


def save_planning(context: RunContext, state: PlanningState) -> None:
    if context.session_manager is not None:
        context.session_manager.append_entry("planning", state=asdict(state))
    context.planning = state


def accept_reply(context: RunContext, reply: str) -> None:
    """Interpret input only in its persisted stage; free text never approves."""
    state = context.planning
    reply = reply.strip()
    if not reply:
        return
    if state.awaiting == "question" and reply == "4":
        save_planning(context, replace(state, awaiting="answer"))
    elif state.awaiting in {"question", "answer"}:
        question = state.questions[0]
        answer = question["options"][int(reply) - 1] if state.awaiting == "question" and reply in {"1", "2", "3"} else reply
        save_planning(context, replace(
            state, questions=state.questions[1:],
            answers=[*state.answers, {"question": question["question"], "answer": answer}],
            awaiting="question" if len(state.questions) > 1 else "",
        ))
    elif state.awaiting == "approval" and reply == "1":
        save_planning(context, replace(state, active=False, approved=True, awaiting=""))
    elif state.awaiting == "approval" and reply == "2":
        save_planning(context, replace(state, awaiting="feedback"))
    elif state.awaiting in {"approval", "feedback"}:
        save_planning(context, replace(state, feedback=[*state.feedback, reply], awaiting=""))


def collect_replies(context: RunContext) -> Result:
    from penhin.cli import ui

    try:
        while context.planning.awaiting:
            state = context.planning
            if state.awaiting == "question":
                question = state.questions[0]
                options = tuple((str(i), label) for i, label in enumerate(question["options"], 1))
                reply = ui.prompt_plan_choice(question["question"], options + (("4", "其它"),))
            elif state.awaiting == "approval":
                reply = ui.prompt_plan_choice(state.content, (("1", "实施计划"), ("2", "计划有问题")))
            else:
                reply = ui.prompt_text("")
            accept_reply(context, reply)
            if state.awaiting in {"answer", "feedback"}:
                ui.print_planning_text("❯ " + reply)
            elif state.awaiting == "question" and reply in {"1", "2", "3"}:
                ui.print_planning_text("❯ " + state.questions[0]["options"][int(reply) - 1])
            elif context.planning.approved:
                ui.print_planning_text("❯ 实施计划")
    except (EOFError, KeyboardInterrupt, ValueError):
        return Result.success("Planning paused awaiting user input. Do not implement.", data={"awaiting_input": True})
    if context.planning.approved:
        return Result.success("User approved the final plan. Continue implementation under the existing permission policy.", data=asdict(context.planning))
    return Result.success("Use the answers and feedback to continue planning. Ask more questions if needed, then submit one complete plan in a content response. Do not implement before final approval.", data=asdict(context.planning))


def handle_planning_response(context: RunContext, blocks: list[dict]) -> Result:
    """Consume structured assistant output only after the user enters /plan."""
    if not context.planning.active:
        return Result.failure("Planning is not active.", code="planning_inactive")
    try:
        questions, content = parse_response(blocks)
    except (ValueError, TypeError):
        from penhin.cli import ui
        ui.print_error("规划响应格式无效，尚未批准任何修改。请重试。")
        return Result.failure("Invalid planning response. Submit questions or content using the required JSON format.", code="invalid_planning_response")
    save_planning(context, replace(
        context.planning, questions=questions or [],
        content=content if content is not None else context.planning.content,
        awaiting="question" if questions else "approval",
    ))
    result = collect_replies(context)
    # Host feedback is context for the next model turn, not another user decision.
    message = {"role": "user", "content": result.message + "\n" + json.dumps(result.data, ensure_ascii=False)}
    if context.session_manager is not None:
        context.session_manager.append_message(message)
    context.messages.append(message)
    return result
