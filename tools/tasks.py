import json
from typing import Any

from result import Result
from task import task_status
from tools.plans import read_plan


def run_task(task: str, agent_type: str = "general") -> Result:
    from orchestration.service import run_recorded_subagent
    current = current_running_task()
    root_task_id = str(current.get("orchestration_job_id", "")) if current else None
    if root_task_id:
        return run_recorded_subagent(task, agent_type=agent_type, root_task_id=root_task_id)
    return run_recorded_subagent(task, agent_type=agent_type)


def run_verify(
    goal: str,
    plan: str = "",
    plan_slug: str = "",
    changes: str = "",
    test_hint: str = "",
) -> Result:
    from orchestration.service import run_recorded_subagent

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
    root_task_id = str(linked_task.get("orchestration_job_id", "")) if linked_task else None
    if root_task_id:
        result = run_recorded_subagent(
            "\n\n".join(sections), agent_type="verification", root_task_id=root_task_id,
        )
    else:
        result = run_recorded_subagent("\n\n".join(sections), agent_type="verification")
    if result.ok and linked_task and linked_plan_slug and plan_content:
        task_status.mark_plan_verified(int(linked_task["id"]), linked_plan_slug)
    return result


def current_running_task() -> dict[str, Any] | None:
    result = task_status(action="show")
    if not result.ok or result.data is None:
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

    orchestration_job_id = ""
    try:
        from auth.secrets import redact_text
        from orchestration.service import repository_from_env
        from orchestration.models import JobStatus

        repository = repository_from_env()
        if repository is not None:
            root_job = repository.create_root_job(
                redact_text(subject), redact_text(description or subject), status=JobStatus.SUCCEEDED,
            )
            orchestration_job_id = root_job.id
    except Exception as error:
        return Result.failure(f"Task was not started because orchestration storage failed: {error}", code="orchestration_unavailable")

    result = task_status(
        action="start",
        subject=subject,
        description=description,
        note=note,
        plan_slug=plan_slug,
        orchestration_job_id=orchestration_job_id,
        todos=plan,
    )

    if not result.ok:
        return result

    return result


def run_task_show(id: int = None) -> Result:
    result = task_status(action="show", id=id)
    if not result.ok or result.data is None:
        return result

    data = dict(result.data)
    data["todos"] = [
        {"index": index, "text": todo["text"], "done": bool(todo["done"])}
        for index, todo in enumerate(data.get("todos", []), start=1)
    ]
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


def run_todo(action: str, items: list[str] | None = None, index: int | None = None) -> Result:
    return task_status.update_todos(action, items=items, index=index)


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
