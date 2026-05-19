import json
from typing import Any
from pathlib import Path

from result import Result


TODO_FILE = Path(".penhin_todos.json")


class TodoManager:
    def __init__(self, todo_file: Path):
        self.todo_file = todo_file
        self.todos: list[dict[str, Any]] = []

    def load(self) -> None:
        if not self.todo_file.exists():
            self.todos = []
            return
        self.todos = json.loads(self.todo_file.read_text("utf-8"))

    def save(self) -> None:
        self.todo_file.write_text(
            json.dumps(self.todos, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def format(self) -> str:
        if not self.todos:
            return "(no todos)"

        lines = []
        for index, todo in enumerate(self.todos, start=1):
            marker = "x" if todo["done"] else " "
            lines.append(f"{index}. [{marker}] {todo['text']}")
        return "\n".join(lines)

    def data(self) -> list[dict[str, Any]]:
        return [
            {
                "index": index,
                "text": todo["text"],
                "done": bool(todo["done"]),
            }
            for index, todo in enumerate(self.todos, start=1)
        ]

    def __call__(self, action: str, items: list[str] = None, index: int = None) -> Result:
        self.load()

        if action == "set":
            if not items:
                return Result.failure("Error: items are required for set", code="missing_items")

            self.todos = [{"text": item, "done": False} for item in items]
            self.save()
            return Result.success(self.format(), data=self.data(), action=action)

        if action == "clear":
            self.todos = []
            self.save()
            return Result.success("Cleared todos", data=self.data(), action=action)

        if action == "show":
            return Result.success(self.format(), data=self.data(), action=action)

        if action == "done":
            if index is None:
                return Result.failure("Error: index is required for done", code="missing_index")
            if index < 1 or index > len(self.todos):
                return Result.failure(f"Error: todo index out of range: {index}", code="index_out_of_range")

            self.todos[index - 1]["done"] = True
            self.save()
            return Result.success(self.format(), data=self.data(), action=action)

        return Result.failure(f"Error: unknown todo action: {action}", code="unknown_action")


run_todo = TodoManager(TODO_FILE)
