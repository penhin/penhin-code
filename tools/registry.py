import json
import sys
import hashlib
from typing import Any

from skills import load_skill
from todo import run_todo

from . import tasks as task_tools
from .shell import run_bash
from .workspace import run_workspace
from .files import run_edit, run_list, run_read, run_search, run_write
from .tasks import (
    run_task,
)
from .types import ApprovalKey, ToolApproval, ToolCategory, ToolInput, ToolSchema, ToolSpec, tool_schema


def _short_digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _input_value_key(name: str) -> ApprovalKey:
    return lambda tool_input: str(tool_input.get(name, ""))


def object_schema(properties: ToolSchema | None = None, required: list[str] | None = None) -> ToolSchema:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }


def _sync_task_status() -> None:
    facade = sys.modules.get("tools")
    if facade is not None and hasattr(facade, "task_status"):
        task_tools.task_status = facade.task_status


def _run_task_status(**kwargs):
    _sync_task_status()
    return task_tools.run_task_status(**kwargs)


def _run_task_start(**kwargs):
    _sync_task_status()
    return task_tools.run_task_start(**kwargs)


def _run_task_show(**kwargs):
    _sync_task_status()
    return task_tools.run_task_show(**kwargs)


def _run_task_complete(**kwargs):
    _sync_task_status()
    return task_tools.run_task_complete(**kwargs)


def _run_background_start(task: str):
    _sync_task_status()
    return task_tools.run_background_start(task)


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
                "plan": {"type": "array", "items": {"type": "string"}},
            },
            ["subject"],
        ),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_start(
            subject=kwargs["subject"],
            description=kwargs.get("description", ""),
            note=kwargs.get("note"),
            plan=kwargs.get("plan"),
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
        handler=lambda **kwargs: _run_task_show(id=kwargs.get("id")),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_complete": ToolSpec(
        name="task_complete",
        description="Mark the current high-level task as completed.",
        input_schema=object_schema({"note": {"type": "string"}}),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_complete(note=kwargs.get("note")),
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
        handler=lambda **kwargs: _run_task_status(
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
        handler=lambda **kwargs: _run_task_status(action="clear"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_list": ToolSpec(
        name="task_list",
        description="List all high-level task states.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_status(action="list"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_switch": ToolSpec(
        name="task_switch",
        description="Switch the current high-level task pointer.",
        input_schema=object_schema({"id": {"type": "integer"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_status(action="switch", id=kwargs["id"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_start": ToolSpec(
        name="background_start",
        description="Start a focused background task and return immediately with its task id.",
        input_schema=object_schema({"task": {"type": "string"}}, ["task"]),
        category=ToolCategory.agent,
        handler=lambda **kwargs: _run_background_start(kwargs["task"]),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(requires_approval=True, key=lambda tool_input: _short_digest(tool_input.get("task", ""))),
    ),
    "background_list": ToolSpec(
        name="background_list",
        description="Show all background tasks and their current statuses.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_status(action="background_list"),
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_show": ToolSpec(
        name="background_show",
        description="Show one background task with its result or error.",
        input_schema=object_schema({"id": {"type": "integer"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: _run_task_status(action="background_show", id=kwargs["id"]),
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
        handler=lambda **kwargs: run_workspace([tool["name"] for tool in PARENT_TOOLS]),
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
