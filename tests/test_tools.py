import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS, ToolCategory
from tools.cache import tool_result_cache
from tools.files import run_list, run_read, run_search, run_write
from tools.glob import run_glob
from tools.shell import command_is_dangerous

from tests.helpers import run_spec_tool


def test_list_ignores_internal_files() -> None:
    output = run_list(".").message
    paths = output.splitlines()

    assert not any(path == ".venv" or path.startswith(".venv/") for path in paths)
    assert not any(path == ".git" or path.startswith(".git/") for path in paths)
    assert not any("__pycache__" in Path(path).parts for path in paths)
    assert ".penhin_todos.json" not in paths
    assert not any(path == ".transcripts" or path.startswith(".transcripts/") for path in paths)
    assert not any(path == ".tasks" or path.startswith(".tasks/") for path in paths)


def test_list_ignored_skills_path_mentions_loader() -> None:
    result = run_list("skills")

    assert result.ok is True
    assert "ignored path: skills" in result.message
    assert "load_skill" in result.message


def test_read_cache_returns_placeholder_for_large_hit() -> None:
    path = Path(".cache-read-test.txt")
    try:
        tool_result_cache.clear()
        path.write_text("x" * 5000, encoding="utf-8")

        first = run_read(str(path), line_numbers=False)
        second = run_read(str(path), line_numbers=False)

        assert first.ok is True
        assert first.message == "x" * 5000
        assert second.ok is True
        assert second.meta["cached"] is True
        assert second.data["cache_hit"] is True
        assert "previous full result" in second.message
    finally:
        tool_result_cache.clear()
        path.unlink(missing_ok=True)


def test_write_invalidates_read_cache() -> None:
    path = Path(".cache-write-test.txt")
    try:
        tool_result_cache.clear()
        path.write_text("before", encoding="utf-8")
        assert run_read(str(path), line_numbers=False).message == "before"
        assert run_read(str(path), line_numbers=False).meta["cached"] is True

        result = run_write(str(path), "after")

        assert result.ok is True
        reread = run_read(str(path), line_numbers=False)
        assert reread.message == "after"
        assert "cached" not in reread.meta
    finally:
        tool_result_cache.clear()
        path.unlink(missing_ok=True)


def test_list_glob_search_cache_hits() -> None:
    root = Path(".cache-tools-test")
    try:
        tool_result_cache.clear()
        root.mkdir(exist_ok=True)
        (root / "alpha.txt").write_text("needle\n", encoding="utf-8")

        assert "cached" not in run_list(str(root)).meta
        assert run_list(str(root)).meta["cached"] is True

        assert "cached" not in run_glob("*.txt", str(root)).meta
        assert run_glob("*.txt", str(root)).meta["cached"] is True

        assert "cached" not in run_search("needle", str(root)).meta
        assert run_search("needle", str(root)).meta["cached"] is True
    finally:
        tool_result_cache.clear()
        for child in root.glob("*"):
            child.unlink()
        root.rmdir()


def test_glob_cache_uses_directory_changes_not_file_content() -> None:
    root = Path(".cache-glob-validator-test")
    try:
        tool_result_cache.clear()
        root.mkdir(exist_ok=True)
        file_path = root / "alpha.py"
        file_path.write_text("before", encoding="utf-8")

        first = run_glob("*.py", str(root))
        file_path.write_text("after", encoding="utf-8")
        second = run_glob("*.py", str(root))

        assert first.ok is True
        assert second.meta["cached"] is True

        (root / "beta.py").write_text("new", encoding="utf-8")
        third = run_glob("*.py", str(root))

        assert third.ok is True
        assert "cached" not in third.meta
        assert "beta.py" in third.message
    finally:
        tool_result_cache.clear()
        for child in root.glob("*"):
            child.unlink()
        root.rmdir()


def test_bash_blocks_dangerous_commands() -> None:
    assert command_is_dangerous("echo ok") is None
    assert command_is_dangerous("git status") is None
    assert command_is_dangerous("rm -rf ./tmp") is None
    assert command_is_dangerous("sudo ls") == "sudo"
    assert command_is_dangerous("echo ok && reboot") == "reboot"
    assert command_is_dangerous("shutdown now") == "shutdown"
    assert command_is_dangerous("rm -rf /") == "rm"
    assert command_is_dangerous("rm -fr / ") == "rm"


def test_workspace_tool() -> None:
    result = run_spec_tool("workspace")
    assert result.ok is True

    data = json.loads(result.message)
    result_data = json.loads(result.to_json())["data"]
    assert "cwd" in data
    assert "git_branch" in data
    assert isinstance(data["dirty_files_count"], int) or data["dirty_files_count"] is None
    assert data["has_agents_md"] is (Path.cwd() / "AGENTS.md").exists()
    assert data["test_command_hint"] == ".venv/bin/python -m pytest -q"
    assert ".venv" in data["ignored"]
    assert ".penhin_todos.json" in data["ignored"]
    assert "workspace" in data["tools"]
    assert "task" in data["tools"]
    assert result_data["cwd"] == data["cwd"]
    assert result_data["dirty_files_count"] == data["dirty_files_count"]


def test_task_tool_registered() -> None:
    assert TOOL_SPECS["task"].handler is not None
    assert "Use verify" in TOOL_SPECS["task"].description
    assert "isolated context" in TOOL_SPECS["task"].description
    assert TOOL_SPECS["task"].input_schema["properties"]["agent_type"]["enum"] == ["explore", "general", "plan"]
    assert "plan_slug" in TOOL_SPECS["task_start"].input_schema["properties"]


def test_verify_tool_registered() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}
    schema = TOOL_SPECS["verify"].input_schema

    assert "verify" in parent_tool_names
    assert "verify" not in child_tool_names
    assert TOOL_SPECS["verify"].handler is not None
    assert "saved plan is loaded automatically" in TOOL_SPECS["verify"].description
    assert schema["required"] == ["goal"]
    assert {"goal", "plan", "plan_slug", "changes", "test_hint"} <= set(schema["properties"])


def test_task_start_with_plan_sets_todos() -> None:
    result = run_spec_tool(
        "task_start",
        subject="Build workflow",
        plan=["inspect code", "make change", "run smoke"],
    )
    todos = run_spec_tool("todo_show")

    assert result.ok is True
    assert todos.ok is True
    assert "1. [ ] inspect code" in todos.message
    assert "2. [ ] make change" in todos.message
    assert "3. [ ] run smoke" in todos.message

    run_spec_tool("task_complete")


def test_task_show_includes_current_todos() -> None:
    run_spec_tool(
        "task_start",
        subject="Show planner state",
        plan=["inspect code", "run smoke"],
    )

    result = run_spec_tool("task_show")

    assert result.ok is True
    assert result.data["subject"] == "Show planner state"
    assert result.data["todos"] == [
        {"index": 1, "text": "inspect code", "done": False},
        {"index": 2, "text": "run smoke", "done": False},
    ]
    assert "Show planner state" in result.message
    assert "inspect code" in result.message

    run_spec_tool("task_complete")


def test_task_complete_includes_todo_summary() -> None:
    run_spec_tool(
        "task_start",
        subject="Complete planner state",
        plan=["inspect code", "run smoke"],
    )
    run_spec_tool("todo_done", index=1)

    result = run_spec_tool("task_complete", note="done")

    assert result.ok is True
    assert result.data["subject"] == "Complete planner state"
    assert result.data["status"] == "completed"
    assert result.data["todos"] == [
        {"index": 1, "text": "inspect code", "done": True},
        {"index": 2, "text": "run smoke", "done": False},
    ]
    assert result.data["todo_summary"] == {
        "total": 2,
        "done": 1,
        "remaining": 1,
    }

def test_task_start_rejects_when_current_task_is_running() -> None:
    first = run_spec_tool("task_start", subject="Current task")
    second = run_spec_tool("task_start", subject="Accidental workflow step")

    assert first.ok is True
    assert second.ok is False
    assert second.meta["code"] == "task_already_running"
    assert second.data["current_task"]["subject"] == "Current task"

    run_spec_tool("task_complete")


def test_tool_schemas_match_handlers() -> None:
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    handler_names = {
        name for name, spec in TOOL_SPECS.items()
        if spec.handler is not None
    }
    spec_names = set(TOOL_SPECS)
    approval_tool_names = {
        name for name, spec in TOOL_SPECS.items()
        if spec.approval.requires_approval
    }

    assert child_tool_names <= spec_names
    assert child_tool_names == {name for name, spec in TOOL_SPECS.items() if spec.available_to_child}
    assert parent_tool_names == {name for name, spec in TOOL_SPECS.items() if spec.available_to_parent}
    assert child_tool_names | parent_tool_names == spec_names
    assert approval_tool_names == {
        "background_start",
        "bash",
        "integration_verify",
        "write",
        "edit",
    }
    assert "task" in handler_names
    assert "task_start" in handler_names
    assert "task_show" in handler_names
    assert "compact" not in handler_names
    assert "snip" not in handler_names


def test_tool_specs_have_one_category() -> None:
    for spec in TOOL_SPECS.values():
        assert isinstance(spec.category, ToolCategory)


def test_tool_specs_declare_parallel_safety() -> None:
    parallel_safe = {
        name for name, spec in TOOL_SPECS.items()
        if spec.parallel_safe
    }

    assert parallel_safe == {
        "agent_artifact_show",
        "agent_dag_show",
        "agent_job_list",
        "agent_job_show",
        "agent_job_wait",
        "background_list",
        "background_show",
        "glob",
        "integration_show",
        "list",
        "load_skill",
        "read",
        "search",
        "task_show",
        "todo_show",
        "workspace",
    }
    assert all(isinstance(spec.parallel_safe, bool) for spec in TOOL_SPECS.values())
    assert all("parallel_safe" in tool for tool in PARENT_TOOLS)
    assert all("parallel_safe" in tool for tool in CHILD_TOOLS)


def test_compact_tool_is_parent_only() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    assert "compact" in parent_tool_names
    assert "compact" not in child_tool_names
    assert TOOL_SPECS["compact"].handler is None


def test_snip_tool_is_parent_only() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    assert "snip" in parent_tool_names
    assert "snip" not in child_tool_names
    assert TOOL_SPECS["snip"].handler is None


def test_plan_mode_tools_are_registered() -> None:
    assert TOOL_SPECS["enter_plan"].handler is not None
    assert TOOL_SPECS["exit_plan"].handler is not None


def test_plan_mode_tools_are_parent_only() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    assert "enter_plan" in parent_tool_names
    assert "exit_plan" in parent_tool_names
    assert "enter_plan" not in child_tool_names
    assert "exit_plan" not in child_tool_names
