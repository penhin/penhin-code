from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from penhin.infrastructure.atomic_io import read_json, write_json_atomic
from penhin.result import Result


TASKS_DIR = Path.cwd().resolve() / ".tasks"
CURRENT_FILE = "current.json"


@dataclass
class TaskStatus:
    id: int
    subject: str
    description: str = ""
    status: str = "running"
    plan_slug: str = ""
    verified_plan_slug: str = ""
    orchestration_job_id: str = ""
    note: str = ""
    todos: list[dict[str, Any]] = field(default_factory=list)
    created_at: int = field(default_factory=time.time_ns)
    updated_at: int = field(default_factory=time.time_ns)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStatus":
        return cls(
            id=int(data["id"]),
            subject=str(data["subject"]),
            description=str(data["description"]),
            status=str(data["status"]),
            plan_slug=str(data["plan_slug"]),
            verified_plan_slug=str(data["verified_plan_slug"]),
            orchestration_job_id=str(data["orchestration_job_id"]),
            note=str(data["note"]),
            todos=[
                {"text": str(todo["text"]), "done": bool(todo["done"])}
                for todo in data["todos"]
            ],
            created_at=int(data["created_at"]),
            updated_at=int(data["updated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class TaskStatusManager:
    def __init__(self, tasks_dir: Path):
        self._lock = threading.RLock()
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(exist_ok=True)
        self.current_file = self.tasks_dir / CURRENT_FILE
        self._next_id = self._max_id() + 1

    def _task_path(self, task_id: int) -> Path:
        return self.tasks_dir / f"task_{task_id}.json"

    def _max_id(self) -> int:
        ids = [int(path.stem.split("_")[1]) for path in self.tasks_dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _save(self, task: TaskStatus) -> None:
        path = self._task_path(task.id)
        write_json_atomic(path, task.to_dict())

    def _load(self, task_id: int) -> TaskStatus:
        path = self._task_path(task_id)
        if not path.exists():
            raise FileNotFoundError(f"Task {task_id} not found")
        data = read_json(path)
        return TaskStatus.from_dict(data)

    def _set_current_id(self, task_id: int | None) -> None:
        if task_id is None:
            self.current_file.unlink(missing_ok=True)
            return
        write_json_atomic(self.current_file, {"current_id": task_id})

    def _get_current_id(self) -> int | None:
        if not self.current_file.exists():
            return None
        data = read_json(self.current_file)
        current_id = data.get("current_id")
        return int(current_id) if current_id is not None else None

    def start(
        self,
        subject: str,
        description: str = "",
        note: str = "",
        plan_slug: str = "",
        orchestration_job_id: str = "",
        todos: list[str] | None = None,
    ) -> TaskStatus:
        with self._lock:
            task = TaskStatus(
                id=self._next_id,
                subject=subject,
                description=description,
                note=note,
                plan_slug=plan_slug,
                orchestration_job_id=orchestration_job_id,
                todos=[{"text": item, "done": False} for item in todos or []],
                status="running",
            )
            self._save(task)
            self._set_current_id(task.id)
            self._next_id += 1
            return task

    def show(self, id: int = None) -> TaskStatus | None:
        with self._lock:
            if id is not None:
                return self._load(id)
            current_id = self._get_current_id()
            if current_id is None:
                return None
            return self._load(current_id)

    def complete(self, note: str = None) -> TaskStatus:
        with self._lock:
            task = self._require_current()
            task.status = "completed"
            if note is not None:
                task.note = note
            task.updated_at = time.time_ns()
            self._save(task)
            self._set_current_id(None)
            return task

    def mark_plan_verified(self, task_id: int, plan_slug: str) -> TaskStatus:
        with self._lock:
            task = self._load(task_id)
            task.verified_plan_slug = plan_slug
            task.updated_at = time.time_ns()
            self._save(task)
            return task

    @staticmethod
    def _todo_data(task: TaskStatus) -> list[dict[str, Any]]:
        return [
            {"index": index, "text": todo["text"], "done": bool(todo["done"])}
            for index, todo in enumerate(task.todos, start=1)
        ]

    @staticmethod
    def _format_todos(task: TaskStatus) -> str:
        if not task.todos:
            return "(no todos)"
        return "\n".join(
            f"{index}. [{'x' if todo['done'] else ' '}] {todo['text']}"
            for index, todo in enumerate(task.todos, start=1)
        )

    def update_todos(
        self,
        action: str,
        items: list[str] | None = None,
        index: int | None = None,
    ) -> Result:
        with self._lock:
            try:
                task = self._require_current()
            except FileNotFoundError as error:
                return Result.failure(f"Error: {error}", code="not_found")

            if action == "set":
                if not items:
                    return Result.failure("Error: items are required for set", code="missing_items")
                task.todos = [{"text": item, "done": False} for item in items]
            elif action == "clear":
                task.todos = []
            elif action == "done":
                if index is None:
                    return Result.failure("Error: index is required for done", code="missing_index")
                if index < 1 or index > len(task.todos):
                    return Result.failure(f"Error: todo index out of range: {index}", code="index_out_of_range")
                task.todos[index - 1]["done"] = True
            elif action != "show":
                return Result.failure(f"Error: unknown todo action: {action}", code="unknown_action")

            if action != "show":
                task.updated_at = time.time_ns()
                self._save(task)
            message = "Cleared todos" if action == "clear" else self._format_todos(task)
            return Result.success(message, data=self._todo_data(task), action=action)

    def _require_current(self) -> TaskStatus:
        task = self.show()
        if task is None:
            raise FileNotFoundError("No current task")
        return task

    def __call__(
        self,
        action: str,
        id: int = None,
        subject: str = None,
        description: str = "",
        note: str = None,
        plan_slug: str = "",
        orchestration_job_id: str = "",
        todos: list[str] | None = None,
    ) -> Result:
        try:
            if action == "start":
                if not subject:
                    return Result.failure("Error: subject is required for start", code="missing_subject")
                task = self.start(subject, description, note or "", plan_slug, orchestration_job_id, todos)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "show":
                task = self.show(id)
                if task is None:
                    return Result.success("(no current task)", data=None, action=action)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "complete":
                task = self.complete(note)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            return Result.failure(f"Error: unknown task action: {action}", code="unknown_action")
        except FileNotFoundError as error:
            return Result.failure(f"Error: {error}", code="not_found")


task_status = TaskStatusManager(TASKS_DIR)
