from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from result import Result


TASKS_DIR = Path(".tasks")
CURRENT_FILE = "current.json"


@dataclass
class TaskStatus:
    id: int
    subject: str
    kind: str = "main"
    description: str = ""
    status: str = "running"
    blocked_by: list[int] = field(default_factory=list)
    note: str = ""
    error: str = ""
    result: str = ""
    created_at: int = field(default_factory=time.time_ns)
    updated_at: int = field(default_factory=time.time_ns)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskStatus":
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
            note=str(data.get("note", "")),
            error=str(data.get("error", "")),
            result=str(data.get("result", "")),
            created_at=int(data.get("created_at", time.time_ns())),
            updated_at=int(data.get("updated_at", time.time_ns())),
        )

    def mark(self, status: str, blocked_by: list[int] = None, note: str = None) -> None:
        self.status = status
        if blocked_by is not None:
            self.blocked_by = sorted(set(blocked_by))
        if note is not None:
            self.note = note
        self.updated_at = time.time_ns()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class TaskStatusManager:
    def __init__(self, tasks_dir: Path):
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
        self._task_path(task.id).write_text(task.to_json(), encoding="utf-8")

    def _load(self, task_id: int) -> TaskStatus:
        path = self._task_path(task_id)
        if not path.exists():
            raise FileNotFoundError(f"Task {task_id} not found")
        return TaskStatus.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _set_current_id(self, task_id: int | None) -> None:
        if task_id is None:
            self.current_file.unlink(missing_ok=True)
            return
        self.current_file.write_text(
            json.dumps({"current_id": task_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_current_id(self) -> int | None:
        if not self.current_file.exists():
            return None
        data = json.loads(self.current_file.read_text(encoding="utf-8"))
        current_id = data.get("current_id")
        return int(current_id) if current_id is not None else None

    def start(self, subject: str, description: str = "", note: str = "") -> TaskStatus:
        task = TaskStatus(
            id=self._next_id,
            subject=subject,
            description=description,
            note=note,
            status="running",
        )
        self._save(task)
        self._set_current_id(task.id)
        self._next_id += 1
        return task

    def start_background(self, subject: str, description: str = "", note: str = "") -> TaskStatus:
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
        if id is not None:
            return self._load(id)
        current_id = self._get_current_id()
        if current_id is None:
            return None
        return self._load(current_id)

    def complete(self, note: str = None) -> TaskStatus:
        task = self._require_current()
        task.mark("completed", blocked_by=[], note=note)
        self._save(task)
        return task

    def block(self, blocked_by: list[int] = None, note: str = None) -> TaskStatus:
        task = self._require_current()
        task.mark("blocked", blocked_by=blocked_by or [], note=note)
        self._save(task)
        return task

    def switch(self, id: int) -> TaskStatus | None:
        if id is None:
            return None
        task = self._load(id)
        self._set_current_id(id)
        return task

    def finish_background(self, id: int, status: str, result: str = "", error: str = "") -> TaskStatus:
        task = self._load(id)
        if task.kind != "background":
            raise FileNotFoundError(f"Background task {id} not found")
        task.status = status
        task.result = result
        task.error = error
        task.updated_at = time.time_ns()
        self._save(task)
        return task

    def list(self, kind: str = None) -> list[dict]:
        tasks = []
        task_paths = sorted(
            self.tasks_dir.glob("task_*.json"),
            key=lambda path: int(path.stem.split("_")[1]),
        )
        for path in task_paths:
            task_id = int(path.stem.split("_")[1])
            task = self._load(task_id).to_dict()
            if kind is not None and task["kind"] != kind:
                continue
            task.pop("description", None)
            task.pop("note", None)
            task.pop("result", None)
            task.pop("error", None)
            tasks.append(task)
        return tasks

    def clear(self) -> None:
        self._set_current_id(None)

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
        blocked_by: list[int] = None,
    ) -> Result:
        try:
            if action == "start":
                if not subject:
                    return Result.failure("Error: subject is required for start", code="missing_subject")
                task = self.start(subject, description, note or "")
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "show":
                task = self.show(id)
                if task is None:
                    return Result.success("(no current task)", data=None, action=action)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "complete":
                task = self.complete(note)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "block":
                task = self.block(blocked_by, note)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "clear":
                self.clear()
                return Result.success("Cleared current task", data={"current_id": None}, action=action)

            if action == "list":
                tasks = self.list()
                return Result.success(json.dumps(tasks, ensure_ascii=False, indent=2), data=tasks, action=action)

            if action == "switch":
                task = self.switch(id)
                return Result.success(task.to_json(), data=task.to_dict(), action=action)

            if action == "background_list":
                tasks = self.list(kind="background")
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
