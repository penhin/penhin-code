import os
import json
import re
import subprocess
import threading
from pathlib import Path

from result import Result
from todo import run_todo
from skills import load_skill
from task import task_status

WORKDIR = Path.cwd()
IGNORED_PATH_PARTS = [".venv", ".git", "__pycache__", ".penhin_todos.json", ".transcripts", ".tasks", "skills"]


CHILD_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current project directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read",
        "description": "Read a file in the current project directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "line_numbers": {"type": "boolean"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": "Write a file in the current project directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list",
        "description": "List all files in the current project directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "edit",
        "description": "Edit a text file by replacing specific content without rewriting the entire file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "search",
        "description": "Search for text, patterns, symbols, or files within the project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "todo_set",
        "description": "Replace the current todo list with ordered items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "todo_show",
        "description": "Show the current todo list.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "todo_done",
        "description": "Mark one todo item as done by 1-based index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
            },
            "required": ["index"],
        },
    },
    {
        "name": "todo_clear",
        "description": "Clear the current todo list.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "workspace",
        "description": "Show the absolute path of the current project working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "load_skill",
        "description": "Load the full content for a skill from skills/<name>/SKILL.md.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
]

PARENT_TOOLS = [
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
            },
            "required": ["task"],
        },
    },   
    {
        "name": "compact",
        "description": "Summarize large context into compact memory representations. Also writes generated transcripts to `.transcripts/`.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "task_start",
        "description": "Start tracking a new current high-level task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_show",
        "description": "Show the current high-level task state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "task_complete",
        "description": "Mark the current high-level task as completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "task_block",
        "description": "Mark the current high-level task as blocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "note": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "task_clear",
        "description": "Clear the current high-level task pointer.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "task_list",
        "description": "list the all high-level task state.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "task_switch",
        "description": "Switch the current high-level task pointer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "background_start",
        "description": "Start a focused background task and return immediately with its task id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "background_list",
        "description": "Show all background tasks and their current statuses.",
        "input_schema": {
            "type": "object",
            "properties": {
            },
            "required": [],
        },
    },
    {
        "name": "background_show",
        "description": "Show one background task with its result or error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
        },
    },
] + CHILD_TOOLS

def is_ignored_path(path: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(WORKDIR).parts
    except ValueError:
        return True

    return any(part in IGNORED_PATH_PARTS for part in relative_parts)


def iter_workspace_files(root: Path):
    if root.is_file():
        yield root
        return

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            dirname for dirname in dirnames
            if not is_ignored_path(current / dirname)
        ]
        for filename in filenames:
            child = current / filename
            if not is_ignored_path(child):
                yield child


def command_references_ignored_path(command: str) -> str | None:
    for part in IGNORED_PATH_PARTS:
        pattern = rf"(^|[\s/\"'`=]){re.escape(part)}($|[\s/\"'`/*])"
        if re.search(pattern, command):
            return part
    return None


def safe_path(path: str) -> Path:
    resolved = (WORKDIR / path).resolve()

    if not resolved.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")

    if is_ignored_path(resolved):
        raise ValueError(f"Path is inside blocked directory: {path}")

    return resolved


def ignored_path_part(path: Path) -> str | None:
    try:
        relative_parts = path.resolve().relative_to(WORKDIR).parts
    except ValueError:
        return None

    for part in relative_parts:
        if part in IGNORED_PATH_PARTS:
            return part
    return None


def run_bash(command: str) -> Result:
    blocked = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(text in command for text in blocked):
        return Result(1, stderr="Error: blocked dangerous command")
    ignored_part = command_references_ignored_path(command)
    if ignored_part:
        return Result(1, stderr=f"Error: command references ignored path: {ignored_part}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return Result(1, stderr="Error: Timeout (30s)")
    except OSError as error:
        return Result(1, stderr=f"Error: {error}")
    return Result(result.returncode, result.stdout, result.stderr)


def run_read(path: str, limit: int = None, line_numbers: bool = True) -> Result:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and len(lines) > limit:
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        if line_numbers:
            lines = [f"{i}: {line}" for i, line in enumerate(lines, start=1)]
        return Result(stdout="\n".join(lines)[:50000])
    except Exception as error:
        return Result(1, stderr=f"Error: {error}")


def run_write(path: str, content: str = None) -> Result:
    try:
        if content is None:
            return Result(1, stderr="Error: content is required")
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return Result(stdout=f"Wrote {len(content)} bytes to {path}")
    except Exception as error:
        return Result(1, stderr=f"Error: {error}")


def run_list(path: str = ".", limit: int = None) -> Result:
    try:
        resolved = (WORKDIR / path).resolve()
        ignored_part = ignored_path_part(resolved)
        if ignored_part:
            hint = " Use load_skill(name=...) for skill instructions." if ignored_part == "skills" else ""
            return Result(stdout=f"(ignored path: {ignored_part}.{hint})")

        file_path = safe_path(path)
        if not file_path.is_dir():
            return Result(1, stderr="Error: Path should be a dir")

        paths = []
        for child in iter_workspace_files(file_path):
            paths.append(str(child.relative_to(WORKDIR)))
            if limit and len(paths) >= limit:
                paths.append("... (limit reached)")
                break

        return Result(stdout="\n".join(paths))

    except Exception as error:
        return Result(1, stderr=f"Error: {error}")


def run_edit(path: str, old: str, new: str) -> Result:
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")

        count = text.count(old)
        if count == 0:
            return Result(1, stderr="Error: old text not found")
        if count > 1:
            return Result(1, stderr=f"Error: old text appears {count}")

        updated = text.replace(old, new, 1)
        file_path.write_text(updated, encoding="utf-8")

        return Result(stdout=f"Edited {path}")
    except Exception as error:
        return Result(1, stderr=f"Error: {error}")


def run_search(query: str, path: str = ".", limit: int = None) -> Result:
    try:
        file_path = safe_path(path)
        results = []

        for child in iter_workspace_files(file_path):
            if limit and len(results) >= limit:
                results.append("... (limit reached)")
                break

            try:
                text = child.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    relative = child.relative_to(WORKDIR)
                    results.append(f"{relative}:{line_number}:{line}")
                    if limit and len(results) >= limit:
                        break

        return Result(stdout="\n".join(results))
    except Exception as error:
        return Result(1, stderr=f"Error: {error}")

def run_task(task: str) -> Result:
    from subagent import run_subagent
    return run_subagent(task)


def run_task_status(**kwargs) -> Result:
    return task_status(**kwargs)


def finish_background_task(task_id: int, task: str) -> None:
    try:
        from subagent import run_subagent

        result = run_subagent(task)
        status = "completed" if result.exit_code == 0 else "failed"
        task_status.finish_background(task_id, status, result.stdout, result.stderr)
    except Exception as error:
        task_status.finish_background(task_id, "failed", error=str(error))


def run_background_start(task: str) -> Result:
    background_task = task_status.start_background(task)
    thread = threading.Thread(
        target=finish_background_task,
        args=(background_task.id, task),
        daemon=True,
    )
    thread.start()
    return Result(stdout=background_task.to_json())


def run_workspace() -> Result:
    info = {
        "cwd": str(WORKDIR),
        "ignored": IGNORED_PATH_PARTS,
        "tools": [tool["name"] for tool in PARENT_TOOLS],
    }
    return Result(stdout=json.dumps(info, ensure_ascii=False, indent=2))

TOOL_HANDLERS = {
    "bash":       lambda **kwargs: run_bash(kwargs["command"]),
    "read":       lambda **kwargs: run_read(
        kwargs["path"], kwargs.get("limit"), kwargs.get("line_numbers", True)
    ),
    "list":       lambda **kwargs: run_list(kwargs.get("path", "."), kwargs.get("limit")),
    "edit":       lambda **kwargs: run_edit(kwargs["path"], kwargs["old"], kwargs["new"]),
    "write":      lambda **kwargs: run_write(kwargs["path"], kwargs["content"]),
    "search":     lambda **kwargs: run_search(
        kwargs["query"], kwargs.get("path", "."), kwargs.get("limit")
    ),
    "todo_set":   lambda **kwargs: run_todo("set", kwargs["items"]),
    "todo_show":  lambda **kwargs: run_todo("show"),
    "todo_done":  lambda **kwargs: run_todo("done", index=kwargs["index"]),
    "todo_clear": lambda **kwargs: run_todo("clear"),
    "workspace":  lambda **kwargs: run_workspace(),
    "load_skill": lambda **kwargs: load_skill(kwargs["name"]),
    "task":       lambda **kwargs: run_task(kwargs["task"]),
    "task_start": lambda **kwargs: run_task_status(
        action="start",
        subject=kwargs["subject"],
        description=kwargs.get("description", ""),
        note=kwargs.get("note"),
    ),
    "task_show": lambda **kwargs: run_task_status(action="show", id=kwargs.get("id")),
    "task_complete": lambda **kwargs: run_task_status(
        action="complete",
        note=kwargs.get("note"),
    ),
    "task_block": lambda **kwargs: run_task_status(
        action="block",
        blocked_by=kwargs.get("blocked_by"),
        note=kwargs.get("note"),
    ),
    "task_clear": lambda **kwargs: run_task_status(action="clear"),
    "task_list":  lambda **kwargs: run_task_status(action="list"),
    "task_switch": lambda **kwargs: run_task_status(action="switch", id=kwargs["id"]),
    "background_start": lambda **kwargs: run_background_start(kwargs["task"]),
    "background_list": lambda **kwargs: run_task_status(action="background_list"),
    "background_show": lambda **kwargs: run_task_status(
        action="background_show",
        id=kwargs["id"],
    ),
}
