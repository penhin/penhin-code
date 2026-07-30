import hashlib
import json
from typing import Any

from skills import load_skill
from . import tasks as task_tools
from .files import run_edit, run_list, run_read, run_search, run_write
from .glob import run_glob
from .orchestration import (
    run_agent_artifact_show,
    run_agent_dag_show,
    run_agent_dag_finalize,
    run_agent_job_cancel,
    run_agent_job_list,
    run_agent_job_show,
    run_agent_job_wait,
    run_agent_plan_create,
    run_integration_show,
    run_integration_start,
    run_integration_verify,
)
from .plan_mode import run_enter_plan, run_exit_plan
from .shell import run_bash
from .types import ApprovalKey, ToolApproval, ToolCategory, ToolSchema, ToolSpec, tool_schema
from .workspace import run_workspace


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


TOOL_SPECS: dict[str, ToolSpec] = {
    "task": ToolSpec(
        name="task",
        description=(
            "Spawn a subagent with an isolated context. "
            "Use agent_type='explore' for read-only codebase investigation, "
            "'plan' for read-only software architecture planning, "
            "and 'general' only when implementation-level tools are needed. "
            "Use verify for completed-work verification."
        ),
        input_schema=object_schema(
            {
                "task": {"type": "string"},
                "agent_type": {
                    "type": "string",
                    "enum": ["explore", "general", "plan"],
                    "description": "Subagent type. Defaults to general when omitted.",
                },
            },
            ["task"],
        ),
        category=ToolCategory.agent,
        handler=lambda **kwargs: task_tools.run_task(
            kwargs["task"],
            agent_type=kwargs.get("agent_type", "general"),
        ),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "verify": ToolSpec(
        name="verify",
        description=(
            "Run a verification agent to check completed coding work. "
            "It may inspect files and run focused checks, but must not modify files. "
            "If plan_slug is provided, or the current task has one, the saved plan is loaded automatically."
        ),
        input_schema=object_schema(
            {
                "goal": {"type": "string"},
                "plan": {"type": "string"},
                "plan_slug": {"type": "string"},
                "changes": {"type": "string"},
                "test_hint": {"type": "string"},
            },
            ["goal"],
        ),
        category=ToolCategory.agent,
        handler=lambda **kwargs: task_tools.run_verify(
            goal=kwargs["goal"],
            plan=kwargs.get("plan", ""),
            plan_slug=kwargs.get("plan_slug", ""),
            changes=kwargs.get("changes", ""),
            test_hint=kwargs.get("test_hint", ""),
        ),
        parallel_safe=False,
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
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "snip": ToolSpec(
        name="snip",
        description=(
            "Mark selected historical conversation turns as snipped so they are omitted from future API context. "
            "Use selectors like '2' or '2-4'."
        ),
        input_schema=object_schema(
            {
                "selectors": {
                    "description": "Turn selectors, for example ['2'], ['2-4'], or '2 3-4'.",
                },
            },
            ["selectors"],
        ),
        category=ToolCategory.agent,
        handler=None,
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "enter_plan": ToolSpec(
        name="enter_plan",
        description=(
            "Enter read-only plan mode before proposing implementation work. "
            "Saves the previous permission mode so exit_plan can restore it."
        ),
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=run_enter_plan,
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "exit_plan": ToolSpec(
        name="exit_plan",
        description=(
            "Exit plan mode after writing a concrete plan. Saves the plan and "
            "restores the permission mode that was active before enter_plan."
        ),
        input_schema=object_schema(
            {
                "plan_content": {"type": "string"},
            },
            ["plan_content"],
        ),
        category=ToolCategory.state,
        handler=run_exit_plan,
        parallel_safe=False,
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
                "plan_slug": {"type": "string"},
            },
            ["subject"],
        ),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_task_start(
            subject=kwargs["subject"],
            description=kwargs.get("description", ""),
            note=kwargs.get("note"),
            plan=kwargs.get("plan"),
            plan_slug=kwargs.get("plan_slug", ""),
        ),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_show": ToolSpec(
        name="task_show",
        description="Show the current high-level task state.",
        input_schema=object_schema({"id": {"type": "integer"}}),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_task_show(id=kwargs.get("id")),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "task_complete": ToolSpec(
        name="task_complete",
        description="Mark the current high-level task as completed.",
        input_schema=object_schema({"note": {"type": "string"}}),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_task_complete(note=kwargs.get("note")),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_start": ToolSpec(
        name="background_start",
        description="Start a focused background task and return immediately with its task id.",
        input_schema=object_schema({"task": {"type": "string"}}, ["task"]),
        category=ToolCategory.agent,
        handler=lambda **kwargs: task_tools.run_background_start(kwargs["task"]),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(requires_approval=True, key=lambda tool_input: _short_digest(tool_input.get("task", ""))),
    ),
    "background_list": ToolSpec(
        name="background_list",
        description="Show all background tasks and their current statuses.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_background_list(),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "background_show": ToolSpec(
        name="background_show",
        description="Show one background task with its result or error.",
        input_schema=object_schema({"id": {"type": "integer"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_background_show(id=kwargs["id"]),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_job_show": ToolSpec(
        name="agent_job_show",
        description="Show the persistent coordination state for one agent job.",
        input_schema=object_schema({"id": {"type": "string"}}, ["id"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_agent_job_show(kwargs["id"]),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_job_list": ToolSpec(
        name="agent_job_list",
        description="List persistent agent jobs, optionally filtered by root task or status.",
        input_schema=object_schema({"root_task_id": {"type": "string"}, "status": {"type": "string"}}),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_agent_job_list(kwargs.get("root_task_id", ""), kwargs.get("status", "")),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_artifact_show": ToolSpec(
        name="agent_artifact_show",
        description="Show the structured result artifact for a completed agent job.",
        input_schema=object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_agent_artifact_show(kwargs["job_id"]),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_job_cancel": ToolSpec(
        name="agent_job_cancel",
        description="Request cancellation of a queued or running agent job.",
        input_schema=object_schema({"id": {"type": "string"}}, ["id"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_agent_job_cancel(kwargs["id"]),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_plan_create": ToolSpec(
        name="agent_plan_create",
        description="Run the Planner and materialize its validated penhin.dag/v1 plan as persistent agent jobs.",
        input_schema=object_schema({"goal": {"type": "string"}}, ["goal"]),
        category=ToolCategory.agent,
        handler=lambda **kwargs: run_agent_plan_create(kwargs["goal"]),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_dag_show": ToolSpec(
        name="agent_dag_show",
        description="Show DAG jobs, dependencies, and which queued jobs are ready to run.",
        input_schema=object_schema({"root_task_id": {"type": "string"}}, ["root_task_id"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_agent_dag_show(kwargs["root_task_id"]),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "agent_dag_finalize": ToolSpec(
        name="agent_dag_finalize",
        description="Finalize completed DAG outputs into one isolated integration worktree and optionally verify it. It never updates main.",
        input_schema=object_schema({
            "root_task_id": {"type": "string"},
            "final_job_ids": {"type": "array", "items": {"type": "string"}},
            "command": {"type": "array", "items": {"type": "string"}},
        }, ["root_task_id", "final_job_ids"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_agent_dag_finalize(
            kwargs["root_task_id"], kwargs["final_job_ids"], kwargs.get("command"),
        ),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(requires_approval=True, key=_input_value_key("command")),
    ),
    "agent_job_wait": ToolSpec(
        name="agent_job_wait",
        description="Wait for a persistent agent job and return its result artifact when complete.",
        input_schema=object_schema({"id": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, ["id"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_agent_job_wait(kwargs["id"], kwargs.get("timeout_seconds", 30)),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "integration_start": ToolSpec(
        name="integration_start",
        description="Create a dedicated integration worktree and cherry-pick ordered, committed Agent change sets. It never updates main.",
        input_schema=object_schema({"root_task_id": {"type": "string"}, "job_ids": {"type": "array", "items": {"type": "string"}}}, ["root_task_id", "job_ids"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_integration_start(kwargs["root_task_id"], kwargs["job_ids"]),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "integration_show": ToolSpec(
        name="integration_show",
        description="Show an integration run, its source Jobs, commit order, and conflict state.",
        input_schema=object_schema({"id": {"type": "string"}}, ["id"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_integration_show(kwargs["id"]),
        parallel_safe=True,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(),
    ),
    "integration_verify": ToolSpec(
        name="integration_verify",
        description="Run an explicit verification command in an integrated worktree. A verified result remains separate from main.",
        input_schema=object_schema({"id": {"type": "string"}, "command": {"type": "array", "items": {"type": "string"}}}, ["id", "command"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: run_integration_verify(kwargs["id"], kwargs["command"]),
        parallel_safe=False,
        available_to_child=False,
        available_to_parent=True,
        approval=ToolApproval(requires_approval=True, key=_input_value_key("command")),
    ),
    "bash": ToolSpec(
        name="bash",
        description="Run a shell command in the current project directory.",
        input_schema=object_schema({"command": {"type": "string"}}, ["command"]),
        category=ToolCategory.shell,
        handler=lambda **kwargs: run_bash(kwargs["command"]),
        parallel_safe=False,
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
        parallel_safe=True,
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
        parallel_safe=False,
        approval=ToolApproval(requires_approval=True, key=_input_value_key("path")),
    ),
    "list": ToolSpec(
        name="list",
        description="List all files in the current project directory.",
        input_schema=object_schema({"path": {"type": "string"}, "limit": {"type": "integer"}}),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_list(kwargs.get("path", "."), kwargs.get("limit")),
        parallel_safe=True,
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
        parallel_safe=False,
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
                "timeout": {"type": "integer"},
            },
            ["query"],
        ),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_search(
            kwargs["query"], kwargs.get("path", "."), kwargs.get("limit"), kwargs.get("timeout", 30)
        ),
        parallel_safe=True,
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
        handler=lambda **kwargs: task_tools.run_todo("set", kwargs["items"]),
        parallel_safe=False,
        approval=ToolApproval(),
    ),
    "todo_show": ToolSpec(
        name="todo_show",
        description="Show the current todo list.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_todo("show"),
        parallel_safe=True,
        approval=ToolApproval(),
    ),
    "todo_done": ToolSpec(
        name="todo_done",
        description="Mark one todo item as done by 1-based index.",
        input_schema=object_schema({"index": {"type": "integer"}}, ["index"]),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_todo("done", index=kwargs["index"]),
        parallel_safe=False,
        approval=ToolApproval(),
    ),
    "todo_clear": ToolSpec(
        name="todo_clear",
        description="Clear the current todo list.",
        input_schema=object_schema(),
        category=ToolCategory.state,
        handler=lambda **kwargs: task_tools.run_todo("clear"),
        parallel_safe=False,
        approval=ToolApproval(),
    ),
    "glob": ToolSpec(
        name="glob",
        description="Search for files by pattern (e.g. **/*.py, src/**/*.ts). Supports recursive ** matching.",
        input_schema=object_schema(
            {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            ["pattern"],
        ),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_glob(kwargs["pattern"], kwargs.get("path", ".")),
        parallel_safe=True,
        approval=ToolApproval(),
    ),
    "workspace": ToolSpec(
        name="workspace",
        description="Show the absolute path of the current project working directory.",
        input_schema=object_schema(),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: run_workspace([tool["name"] for tool in PARENT_TOOLS]),
        parallel_safe=True,
        approval=ToolApproval(),
    ),
    "load_skill": ToolSpec(
        name="load_skill",
        description="Load a user-provided skill from skills/<name>/SKILL.md.",
        input_schema=object_schema({"name": {"type": "string"}}, ["name"]),
        category=ToolCategory.readonly,
        handler=lambda **kwargs: load_skill(kwargs["name"]),
        parallel_safe=True,
        approval=ToolApproval(),
    ),
}


CHILD_TOOLS = [tool_schema(spec) for spec in TOOL_SPECS.values() if spec.available_to_child]
PARENT_TOOLS = [tool_schema(spec) for spec in TOOL_SPECS.values() if spec.available_to_parent]


def tool_description_lines(tools: list[ToolSchema] = PARENT_TOOLS) -> list[str]:
    return [f"- {tool['name']}: {tool['description']}" for tool in tools]


def tool_names(tools: list[ToolSchema] = PARENT_TOOLS) -> list[str]:
    return [tool["name"] for tool in tools]
