"""Persisted planning dialogue, independent of the tool permission policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from penhin.result import Result
from penhin.tools.builtin.planning import PlanningQuestion, valid_plan_input

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
    return Result.success("Use the answers and feedback to continue planning. Ask more questions if needed, then present one complete plan using content. Do not implement before final approval.", data=asdict(context.planning))


def present_plan(context: RunContext | None, questions: list[PlanningQuestion] | None, content: str | None) -> Result:
    if context is None:
        return Result.failure("No active session for planning.", code="no_context")
    if not valid_plan_input(questions, content):
        return Result.failure("Invalid planning questions or content.", code="invalid_tool_input")
    if context.planning.awaiting:
        return Result.success("Waiting for the user's pending planning response.", data={"awaiting_input": True})
    state = context.planning if context.planning.active else PlanningState(active=True)
    save_planning(context, replace(
        state, active=True, approved=False, questions=questions or [],
        content=content if content is not None else state.content,
        awaiting="question" if questions else "approval" if content else "",
    ))
    if questions is None and content is None:
        return Result.success("Planning started. Inspect with read, ask questions as needed, then submit one complete plan for final approval.")
    return collect_replies(context)
