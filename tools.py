import os
import json
import re
import subprocess
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
        "name": "todo",
        "description": "Manage a small in-memory todo list for multi-step tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "show", "done", "clear"]},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "index": {"type": "integer"},
            },
            "required": ["action"],
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
        "name": "task_status",
        "description": "Track the current high-level task state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "show", "complete", "block", "clear"]},
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "note": {"type": "string"},
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["action"],
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
    "todo":       lambda **kwargs: run_todo(
        kwargs["action"], kwargs.get("items"), kwargs.get("index")
    ),
    "workspace":  lambda **kwargs: run_workspace(),
    "load_skill": lambda **kwargs: load_skill(kwargs["name"]),
    "task":       lambda **kwargs: run_task(kwargs["task"]),
    "task_status": lambda **kwargs: run_task_status(**kwargs),
}
