from . import tasks as _tasks
from .files import (
    ignored_path_part,
    run_edit,
    run_list,
    run_read,
    run_search,
    run_write,
    safe_path,
)
from .registry import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS, object_schema
from .shell import (
    command_is_dangerous,
    command_references_ignored_path,
    command_uses_dangerous_name,
    run_bash,
)
from .tasks import (
    current_todos,
    run_task,
    run_task_complete,
    run_task_show,
    run_task_start,
    todo_summary,
)
from .types import ToolApproval, ToolCategory, ToolInput, ToolSchema, ToolSpec, tool_schema
from .workspace import (
    IGNORED_PATH_PARTS,
    WORKDIR,
    git_branch_name,
    git_dirty_files_count,
    is_ignored_path,
    iter_workspace_files,
    run_git,
    run_workspace as _run_workspace,
    test_command_hint,
    workspace_info as _workspace_info,
)

task_status = _tasks.task_status
threading = _tasks.threading


def run_task_status(**kwargs):
    _tasks.task_status = task_status
    return _tasks.run_task_status(**kwargs)


def run_background_start(task: str):
    _tasks.task_status = task_status
    return _tasks.run_background_start(task)


def workspace_info() -> dict[str, object]:
    return _workspace_info([tool["name"] for tool in PARENT_TOOLS])


def run_workspace():
    return _run_workspace([tool["name"] for tool in PARENT_TOOLS])
