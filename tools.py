import os
import hashlib
import json
import re
import subprocess
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from dataclasses import dataclass, field

from atomic_io import atomic_write_text
from result import Result
from todo import run_todo
from skills import load_skill
from task import task_status

WORKDIR = Path.cwd().resolve()
IGNORED_PATH_PARTS = [".venv", ".git", "__pycache__", ".penhin_todos.json", ".transcripts", ".tasks", "skills"]
BACKGROUND_THREAD_PREFIX = "background-task-"
FILE_LOCK = threading.RLock()
DANGEROUS_COMMAND_NAMES = {"sudo", "shutdown", "reboot"}
DANGEROUS_RM_ROOT = re.compile(r"(^|[;&|]\s*)rm\s+(-[A-Za-z]*[rf][A-Za-z]*\s+)+/(\s|$)")


class ToolCategory(Enum):
    readonly = 0
    state = 1
    write = 2
    shell = 3
    agent = 4


ToolInput = dict[str, Any]
ToolSchema = dict[str, Any]
ApprovalKey = Callable[[ToolInput], str]


def _short_digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _input_value_key(name: str) -> ApprovalKey:
    return lambda tool_input: str(tool_input.get(name, ""))


@dataclass
class ToolApproval:
    requires_approval: bool = False
    key: ApprovalKey | None = None

    def approval_key(self, tool_name: str, tool_input: ToolInput) -> str:
        if self.key is None:
            return tool_name
        return f"{tool_name}:{self.key(tool_input)}"


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: ToolSchema
    category: ToolCategory
    handler: Callable[..., Result] | None
    available_to_child: bool = True
    available_to_parent: bool = True
    approval: ToolApproval = field(default_factory=ToolApproval)


def tool_schema(spec: ToolSpec) -> ToolSchema:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }


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


def command_uses_dangerous_name(command: str) -> str | None:
    for name in DANGEROUS_COMMAND_NAMES:
        pattern = rf"(^|[;&|]\s*){re.escape(name)}(\s|$)"
        if re.search(pattern, command):
            return name
    return None


def command_is_dangerous(command: str) -> str | None:
    dangerous_name = command_uses_dangerous_name(command)
    if dangerous_name:
        return dangerous_name
    if DANGEROUS_RM_ROOT.search(command):
        return "rm"
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
    dangerous_command = command_is_dangerous(command)
    if dangerous_command:
        return Result.failure(
            f"Error: blocked dangerous command: {dangerous_command}",
            code="blocked_command",
            command=dangerous_command,
        )
    ignored_part = command_references_ignored_path(command)
    if ignored_part:
        return Result.failure(
            f"Error: command references ignored path: {ignored_part}",
            code="ignored_path",
            ignored_part=ignored_part,
        )
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return Result.failure("Error: Timeout (30s)", code="timeout", timeout_seconds=30)
    except OSError as error:
        return Result.failure(f"Error: {error}", code="os_error")
    return Result(
        result.returncode,
        result.stdout,
        result.stderr,
        data={
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


def run_read(path: str, limit: int = None, line_numbers: bool = True) -> Result:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and len(lines) > limit:
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        if line_numbers:
            lines = [f"{i}: {line}" for i, line in enumerate(lines, start=1)]
        output = "\n".join(lines)[:50000]
        return Result.success(
            output,
            data={"path": path, "lines": lines, "line_numbers": line_numbers},
            truncated=len(output) >= 50000,
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="read_error")


def run_write(path: str, content: str = None) -> Result:
    try:
        if content is None:
            return Result.failure("Error: content is required", code="missing_content")
        file_path = safe_path(path)
        with FILE_LOCK:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(file_path, content)
        return Result.success(
            f"Wrote {len(content)} bytes to {path}",
            data={"path": path, "bytes": len(content)},
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="write_error")


def run_list(path: str = ".", limit: int = None) -> Result:
    try:
        resolved = (WORKDIR / path).resolve()
        ignored_part = ignored_path_part(resolved)
        if ignored_part:
            hint = " Use load_skill(name=...) for skill instructions." if ignored_part == "skills" else ""
            return Result.success(
                f"(ignored path: {ignored_part}.{hint})",
                data={"path": path, "ignored_part": ignored_part},
            )

        file_path = safe_path(path)
        if not file_path.is_dir():
            return Result.failure("Error: Path should be a dir", code="not_directory", data={"path": path})

        paths = []
        for child in iter_workspace_files(file_path):
            paths.append(str(child.relative_to(WORKDIR)))
            if limit and len(paths) >= limit:
                paths.append("... (limit reached)")
                break

        return Result.success(
            "\n".join(paths),
            data={"path": path, "paths": paths, "limit": limit},
            count=len(paths),
        )

    except Exception as error:
        return Result.failure(f"Error: {error}", code="list_error")


def run_edit(path: str, old: str, new: str) -> Result:
    try:
        file_path = safe_path(path)
        with FILE_LOCK:
            text = file_path.read_text(encoding="utf-8")

            count = text.count(old)
            if count == 0:
                return Result.failure("Error: old text not found", code="old_text_not_found")
            if count > 1:
                return Result.failure(f"Error: old text appears {count}", code="old_text_not_unique", count=count)

            updated = text.replace(old, new, 1)
            atomic_write_text(file_path, updated)

        return Result.success(f"Edited {path}", data={"path": path, "replacements": 1})
    except Exception as error:
        return Result.failure(f"Error: {error}", code="edit_error")


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

        return Result.success(
            "\n".join(results),
            data={"query": query, "path": path, "matches": results, "limit": limit},
            count=len(results),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="search_error")

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


def run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result


def git_branch_name() -> str:
    result = run_git(["branch", "--show-current"])
    if result is None:
        return "-"
    return result.stdout.strip() or "-"


def git_dirty_files_count() -> int | None:
    result = run_git(["status", "--short"])
    if result is None:
        return None
    return len([
        line for line in result.stdout.splitlines()
        if line.strip()
    ])


def test_command_hint() -> str:
    if (WORKDIR / "tests" / "test_smoke.py").exists():
        return ".venv/bin/python tests/test_smoke.py"
    if (WORKDIR / "pytest.ini").exists() or (WORKDIR / "pyproject.toml").exists():
        return "pytest"
    return "-"


def workspace_info() -> dict[str, object]:
    return {
        "cwd": str(WORKDIR),
        "git_branch": git_branch_name(),
        "dirty_files_count": git_dirty_files_count(),
        "has_agents_md": (WORKDIR / "AGENTS.md").exists(),
        "ignored": IGNORED_PATH_PARTS,
        "test_command_hint": test_command_hint(),
        "tools": [tool["name"] for tool in PARENT_TOOLS],
    }


def run_workspace() -> Result:
    info = workspace_info()
    return Result.success(json.dumps(info, ensure_ascii=False, indent=2), data=info)

def object_schema(properties: ToolSchema | None = None, required: list[str] | None = None) -> ToolSchema:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }


TOOL_SPECS: dict[str, ToolSpec] = {
    "task": ToolSpec(
        name="task",
        description="Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        input_schema=object_schema({"task": {"type": "string"}}, ["task"]),
        category=ToolCategory.agent,
        handler=lambda **kwargs: run_task(kwargs["task"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "compact": ToolSpec(
        name="compact",
        description="Summarize large context into compact memory representations. Also writes generated transcripts to `.transcripts/`.",
        input_schema=object_schema(),
        category=ToolCategory.agent,
        handler=None,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_start": ToolSpec(
        name="task_start",
        description="Start tracking a new current high-level task.",
        input_schema=object_schema(
            {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "note": {"type": "string"},
            },
            ["subject"],
        ),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(
            action="start",
            subject=kwargs["subject"],
            description=kwargs.get("description", ""),
            note=kwargs.get("note"),
        ),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_show": ToolSpec(
        name="task_show",
        description="Show the current high-level task state.",
        input_schema=object_schema({"id": {"type": "integer"}}),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="show", id=kwargs.get("id")),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_complete": ToolSpec(
        name="task_complete",
        description="Mark the current high-level task as completed.",
        input_schema=object_schema({"note": {"type": "string"}}),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="complete", note=kwargs.get("note")),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_block": ToolSpec(
        name="task_block",
        description="Mark the current high-level task as blocked.",
        input_schema=object_schema(
            {
                "blocked_by": {"type": "array", "items": {"type": "integer"}},
                "note": {"type": "string"},
            }
        ),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(
            action="block",
            blocked_by=kwargs.get("blocked_by"),
            note=kwargs.get("note"),
        ),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_clear": ToolSpec(
        name="task_clear",
        description="Clear the current high-level task pointer.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="clear"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_list": ToolSpec(
        name="task_list",
        description="List all high-level task states.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="list"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_switch": ToolSpec(
        name="task_switch",
        description="Switch the current high-level task pointer.",
        input_schema=object_schema({"id": {"type": "integer"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="switch", id=kwargs["id"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_start": ToolSpec(
        name="background_start",
        description="Start a focused background task and return immediately with its task id.",
        input_schema=object_schema({"task": {"type": "string"}}, ["task"]),
        category=ToolCategory.agent,
        handler=lambda **kwargs: run_background_start(kwargs["task"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(requires_approval=True, key=lambda tool_input: _short_digest(tool_input.get("task", ""))),
    ),
    "background_list": ToolSpec(
        name="background_list",
        description="Show all background tasks and their current statuses.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="background_list"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_show": ToolSpec(
        name="background_show",
        description="Show one background task with its result or error.",
        input_schema=object_schema({"id": {"type": "integer"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_task_status(action="background_show", id=kwargs["id"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "bash": ToolSpec(
        name="bash",
        description="Run a shell command in the current project directory.",
        input_schema=object_schema({"command": {"type": "string"}}, ["command"]),
        category=ToolCategory.shell,
        handler=lambda **kwargs: run_bash(kwargs["command"]),
        approval=ToolApproval(requires_approval=True, key=_input_value_key("command")),
    ),
    "read": ToolSpec(
        name="read",
        description="Read a file in the current project directory.",
        input_schema=object_schema(
            {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "line_numbers": {"type": "boolean"},
            },
            ["path"],
        ),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_read(
            kwargs["path"], kwargs.get("limit"), kwargs.get("line_numbers", True)
        ),
        approval=ToolApproval(),
    ),
    "write": ToolSpec(
        name="write",
        description="Write a file in the current project directory.",
        input_schema=object_schema(
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        category=ToolCategory.write,
        handler=lambda **kwargs: run_write(kwargs["path"], kwargs["content"]),
        approval=ToolApproval(requires_approval=True, key=_input_value_key("path")),
    ),
    "list": ToolSpec(
        name="list",
        description="List all files in the current project directory.",
        input_schema=object_schema({"path": {"type": "string"}, "limit": {"type": "integer"}}),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_list(kwargs.get("path", "."), kwargs.get("limit")),
        approval=ToolApproval(),
    ),
    "edit": ToolSpec(
        name="edit",
        description="Edit a text file by replacing specific content without rewriting the entire file.",
        input_schema=object_schema(
            {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            ["path", "old", "new"],
        ),
        category=ToolCategory.write,
        handler=lambda **kwargs: run_edit(kwargs["path"], kwargs["old"], kwargs["new"]),
        approval=ToolApproval(requires_approval=True, key=_input_value_key("path")),
    ),
    "search": ToolSpec(
        name="search",
        description="Search for text, patterns, symbols, or files within the project.",
        input_schema=object_schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            ["query"],
        ),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_search(
            kwargs["query"], kwargs.get("path", "."), kwargs.get("limit")
        ),
        approval=ToolApproval(),
    ),
    "todo_set": ToolSpec(
        name="todo_set",
        description="Replace the current todo list with ordered items.",
        input_schema=object_schema(
            {"items": {"type": "array", "items": {"type": "string"}}},
            ["items"],
        ),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_todo("set", kwargs["items"]),
        approval=ToolApproval(),
    ),
    "todo_show": ToolSpec(
        name="todo_show",
        description="Show the current todo list.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_todo("show"),
        approval=ToolApproval(),
    ),
    "todo_done": ToolSpec(
        name="todo_done",
        description="Mark one todo item as done by 1-based index.",
        input_schema=object_schema({"index": {"type": "integer"}}, ["index"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_todo("done", index=kwargs["index"]),
        approval=ToolApproval(),
    ),
    "todo_clear": ToolSpec(
        name="todo_clear",
        description="Clear the current todo list.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_todo("clear"),
        approval=ToolApproval(),
    ),
    "workspace": ToolSpec(
        name="workspace",
        description="Show the absolute path of the current project working directory.",
        input_schema=object_schema(),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_workspace(),
        approval=ToolApproval(),
    ),
    "load_skill": ToolSpec(
        name="load_skill",
        description="Load the full content for a skill from skills/<name>/SKILL.md.",
        input_schema=object_schema({"name": {"type": "string"}}, ["name"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: load_skill(kwargs["name"]),
        approval=ToolApproval(),
    ),
}


CHILD_TOOLS = [tool_schema(spec) for spec in TOOL_SPECS.values() if spec.available_to_child]
PARENT_TOOLS = [tool_schema(spec) for spec in TOOL_SPECS.values() if spec.available_to_parent]
