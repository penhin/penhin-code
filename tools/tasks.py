import json
import threading
from typing import Any

from result import Result
from task import task_status
from todo import run_todo
from tools.plans import read_plan


BACKGROUND_THREAD_PREFIX = "background-task-"


def run_task(task: str) -> Result:
    from subagent import run_subagent
    return run_subagent(task, agent_type="general")


def run_verify(
    goal: str,
    plan: str = "",
    plan_slug: str = "",
    changes: str = "",
    test_hint: str = "",
) -> Result:
    from subagent import run_subagent

    plan_content = plan.strip()
    linked_task = current_running_task()
    linked_plan_slug = plan_slug.strip()
    if not plan_content:
        linked_plan_slug = linked_plan_slug or (str(linked_task.get("plan_slug", "")) if linked_task else "")
        if linked_plan_slug:
            plan_content = read_plan(linked_plan_slug) or ""

    sections = [
        "Verify the completed coding work against the request below.",
        f"Goal:\n{goal.strip()}",
    ]
    if plan_content:
        plan_header = f"Plan ({linked_plan_slug}):" if linked_plan_slug else "Plan:"
        sections.append(f"{plan_header}\n{plan_content}")
    elif linked_plan_slug:
        sections.append(f"Associated plan slug could not be loaded:\n{linked_plan_slug}")
    if changes:
        sections.append(f"Changes:\n{changes.strip()}")
    if test_hint:
        sections.append(f"Suggested checks:\n{test_hint.strip()}")

    sections.append(
        "Return a concise verdict with checks run, failures, residual risks, "
        "and recommended next actions. Do not modify files."
    )
    result = run_subagent("\n\n".join(sections), agent_type="verification")
    if result.ok and linked_task and linked_plan_slug and plan_content:
        task_status.mark_plan_verified(int(linked_task["id"]), linked_plan_slug)
    return result


def current_running_task() -> dict[str, Any] | None:
    result = task_status(action="show")
    if not result.ok or result.data is None:
        return None
    if result.data.get("kind") != "main":
        return None
    if result.data.get("status") != "running":
        return None
    return result.data


def run_task_start(
    subject: str,
    description: str = "",
    note: str = None,
    plan: list[str] = None,
    plan_slug: str = "",
) -> Result:
    current = current_running_task()
    if current is not None:
        return Result.failure(
            "Error: current task is still running; complete, block, or clear it before starting a new task",
            code="task_already_running",
            data={"current_task": current},
        )

    result = task_status(
        action="start",
        subject=subject,
        description=description,
        note=note,
        plan_slug=plan_slug,
    )

    if not result.ok:
        return result

    if plan:
        todo_result = run_todo("set", plan)
        if not todo_result.ok:
            return Result.failure(
                f"Task started, but plan setup failed: {todo_result.error}",
                code="task_plan_failed",
                data=result.data,
            )

    return result


def run_task_show(id: int = None) -> Result:
    result = task_status(action="show", id=id)
    if not result.ok or result.data is None:
        return result

    data = dict(result.data)
    data["todos"] = current_todos()
    return Result.success(
        json.dumps(data, ensure_ascii=False, indent=2),
        data=data,
        action="show",
    )


def current_todos() -> list[dict[str, Any]]:
    todos = run_todo("show")
    if not todos.ok:
        return []
    return todos.data


def todo_summary(todos: list[dict[str, Any]]) -> dict[str, int]:
    total = len(todos)
    done = sum(1 for todo in todos if todo["done"])
    return {
        "total": total,
        "done": done,
        "remaining": total - done,
    }


def run_task_complete(note: str = None) -> Result:
    todos = current_todos()
    current = current_running_task()
    plan_slug = str(current.get("plan_slug", "")) if current else ""
    verified_plan_slug = str(current.get("verified_plan_slug", "")) if current else ""
    result = task_status(action="complete", note=note)
    if not result.ok:
        return result

    data = dict(result.data)
    data["todos"] = todos
    data["todo_summary"] = todo_summary(todos)
    data["unverified_plan"] = bool(plan_slug and verified_plan_slug != plan_slug)
    return Result.success(
        json.dumps(data, ensure_ascii=False, indent=2),
        data=data,
        action="complete",
    )


def finish_background_task(task_id: int, task: str) -> None:
    try:
        from subagent import run_subagent

        result = run_subagent(task)
        status = "completed" if result.ok else "failed"
        task_status.finish_background(task_id, status, result.message, result.error)
    except Exception as error:
        task_status.finish_background(task_id, "failed", error=str(error))


def run_background_start(task: str) -> Result:
    if threading.current_thread().name.startswith(BACKGROUND_THREAD_PREFIX):
        return Result.failure(
            "Error: background tasks cannot start nested background tasks",
            code="nested_background_task",
        )

    background_task = task_status.start_background(task)
    thread = threading.Thread(
        target=finish_background_task,
        args=(background_task.id, task),
        daemon=True,
        name=f"{BACKGROUND_THREAD_PREFIX}{background_task.id}",
    )
    thread.start()
    return Result.success(background_task.to_json(), data=background_task.to_dict())
