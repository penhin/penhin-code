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
    description: str = ""
    status: str = "running"
    blocked_by: list[int] = field(default_factory=list)
    note: str = ""
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
            description=str(data.get("description", "")),
            status=str(data.get("status", "running")),
            blocked_by=list(blocked_by),
            note=str(data.get("note", "")),
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

    def show(self) -> TaskStatus | None:
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
        subject: str = None,
        description: str = "",
        note: str = None,
        blocked_by: list[int] = None,
    ) -> Result:
        try:
            if action == "start":
                if not subject:
                    return Result(1, stderr="Error: subject is required for start")
                return Result(stdout=self.start(subject, description, note or "").to_json())

            if action == "show":
                task = self.show()
                return Result(stdout=task.to_json() if task else "(no current task)")

            if action == "complete":
                return Result(stdout=self.complete(note).to_json())

            if action == "block":
                return Result(stdout=self.block(blocked_by, note).to_json())

            if action == "clear":
                self.clear()
                return Result(stdout="Cleared current task")

            return Result(1, stderr=f"Error: unknown task action: {action}")
        except FileNotFoundError as error:
            return Result(1, stderr=f"Error: {error}")


task_status = TaskStatusManager(TASKS_DIR)
