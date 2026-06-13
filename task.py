from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from atomic_io import read_json, write_json_atomic
from result import Result


TASKS_DIR = Path.cwd().resolve() / ".tasks"
CURRENT_FILE = "current.json"


@dataclass
class TaskStatus:
    id: int
    subject: str
    kind: str = "main"
    description: str = ""
    status: str = "running"
    blocked_by: list[int] = field(default_factory=list)
    plan_slug: str = ""
    verified_plan_slug: str = ""
    note: str = ""
    error: str = ""
    result: str = ""
    created_at: int = field(default_factory=time.time_ns)
    updated_at: int = field(default_factory=time.time_ns)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStatus":
        blocked_by = data.get("blocked_by", [])
        if isinstance(blocked_by, int):
            blocked_by = [blocked_by]

        return cls(
            id=int(data["id"]),
            subject=str(data["subject"]),
            kind=str(data.get("kind", "main")),
            description=str(data.get("description", "")),
            status=str(data.get("status", "running")),
            blocked_by=list(blocked_by),
            plan_slug=str(data.get("plan_slug", "")),
            verified_plan_slug=str(data.get("verified_plan_slug", "")),
            note=str(data.get("note", "")),
            error=str(data.get("error", "")),
            result=str(data.get("result", "")),
            created_at=int(data.get("created_at", time.time_ns())),
            updated_at=int(data.get("updated_at", time.time_ns())),
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
        return TaskStatus.from_dict(read_json(path))

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
    ) -> TaskStatus:
        with self._lock:
            task = TaskStatus(
                id=self._next_id,
                subject=subject,
                description=description,
                note=note,
                plan_slug=plan_slug,
                status="running",
            )
            self._save(task)
            self._set_current_id(task.id)
            self._next_id += 1
            return task

    def start_background(self, subject: str, description: str = "", note: str = "") -> TaskStatus:
        with self._lock:
            task = TaskStatus(
                id=self._next_id,
                subject=subject,
                kind="background",
                description=description,
                note=note,
                status="running",
            )
            self._save(task)
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

    def finish_background(self, id: int, status: str, result: str = "", error: str = "") -> TaskStatus:
        with self._lock:
            task = self._load(id)
            if task.kind != "background":
                raise FileNotFoundError(f"Background task {id} not found")
            task.status = status
            task.result = result
            task.error = error
            task.updated_at = time.time_ns()
            self._save(task)
            return task

    def _list_background(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks: list[dict[str, Any]] = []
            for path in sorted(self.tasks_dir.glob("task_*.json"), key=lambda p: int(p.stem.split("_")[1])):
                task = self._load(int(path.stem.split("_")[1])).to_dict()
                if task["kind"] != "background":
                    continue
                task.pop("description", None)
                task.pop("note", None)
                task.pop("result", None)
                task.pop("error", None)
                tasks.append(task)
            return tasks

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
    ) -> Result:
        try:
            if action == "start":
                if not subject:
                    return Result.failure("Error: subject is required for start", code="missing_subject")
                task = self.start(subject, description, note or "", plan_slug)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "show":
                task = self.show(id)
                if task is None:
                    return Result.success("(no current task)", data=None, action=action)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "complete":
                task = self.complete(note)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "background_list":
                tasks = self._list_background()
                return Result.success(json.dumps(tasks, ensure_ascii=False, indent=2), data=tasks, action=action)

            if action == "background_show":
                task = self.show(id)
                if task is None or task.kind != "background":
                    return Result.failure(f"Error: Background task {id} not found", code="not_found")
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            return Result.failure(f"Error: unknown task action: {action}", code="unknown_action")
        except FileNotFoundError as error:
            return Result.failure(f"Error: {error}", code="not_found")


task_status = TaskStatusManager(TASKS_DIR)
