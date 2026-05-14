import json
from pathlib import Path

from result import Result


TODO_FILE = Path(".penhin_todos.json")


class TodoManager:
    def __init__(self, todo_file: Path):
        self.todo_file = todo_file
        self.todos = []

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

    def __call__(self, action: str, items: list[str] = None, index: int = None) -> Result:
        self.load()

        if action == "set":
            if not items:
                return Result(1, stderr="Error: items are required for set")

            self.todos = [{"text": item, "done": False} for item in items]
            self.save()
            return Result(stdout=self.format())

        if action == "clear":
            self.todos = []
            self.save()
            return Result(stdout="Cleared todos")

        if action == "show":
            return Result(stdout=self.format())

        if action == "done":
            if index is None:
                return Result(1, stderr="Error: index is required for done")
            if index < 1 or index > len(self.todos):
                return Result(1, stderr=f"Error: todo index out of range: {index}")

            self.todos[index - 1]["done"] = True
            self.save()
            return Result(stdout=self.format())

        return Result(1, stderr=f"Error: unknown todo action: {action}")


run_todo = TodoManager(TODO_FILE)
