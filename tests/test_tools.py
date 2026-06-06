import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools
from skills import load_skill
from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS, ToolCategory, run_list

from tests.helpers import run_spec_tool


def test_list_ignores_internal_files() -> None:
    output = run_list(".").stdout
    paths = output.splitlines()

    assert not any(path == ".venv" or path.startswith(".venv/") for path in paths)
    assert not any(path == ".git" or path.startswith(".git/") for path in paths)
    assert not any("__pycache__" in Path(path).parts for path in paths)
    assert ".penhin_todos.json" not in paths
    assert not any(path == ".transcripts" or path.startswith(".transcripts/") for path in paths)
    assert not any(path == ".tasks" or path.startswith(".tasks/") for path in paths)


def test_list_ignored_path_returns_hint() -> None:
    result = run_list("skills")

    assert result.exit_code == 0
    assert "ignored path: skills" in result.stdout
    assert "load_skill" in result.stdout


def test_bash_blocks_dangerous_commands() -> None:
    assert tools.command_is_dangerous("echo ok") is None
    assert tools.command_is_dangerous("git status") is None
    assert tools.command_is_dangerous("rm -rf ./tmp") is None
    assert tools.command_is_dangerous("sudo ls") == "sudo"
    assert tools.command_is_dangerous("echo ok && reboot") == "reboot"
    assert tools.command_is_dangerous("shutdown now") == "shutdown"
    assert tools.command_is_dangerous("rm -rf /") == "rm"
    assert tools.command_is_dangerous("rm -fr / ") == "rm"


def test_workspace_tool() -> None:
    result = run_spec_tool("workspace")
    assert result.exit_code == 0

    data = json.loads(result.stdout)
    result_data = json.loads(result.to_json())["data"]
    assert "cwd" in data
    assert "git_branch" in data
    assert isinstance(data["dirty_files_count"], int) or data["dirty_files_count"] is None
    assert data["has_agents_md"] is True
    assert data["test_command_hint"] == ".venv/bin/python tests/test_smoke.py"
    assert ".venv" in data["ignored"]
    assert ".penhin_todos.json" in data["ignored"]
    assert "workspace" in data["tools"]
    assert "task" in data["tools"]
    assert result_data["cwd"] == data["cwd"]
    assert result_data["dirty_files_count"] == data["dirty_files_count"]


def test_skill_loader() -> None:
    descriptions = load_skill.get_descriptions()
    assert "code-review" in descriptions

    content = run_spec_tool("load_skill", name="code-review")
    assert content.exit_code == 0
    assert "<skill name=\"code-review\">" in content.stdout
    assert "Code Review" in content.stdout


def test_task_tool_registered() -> None:
    assert TOOL_SPECS["task"].handler is not None


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
        "write",
        "edit",
    }
    assert "task" in handler_names
    assert "task_start" in handler_names
    assert "task_show" in handler_names
    assert "compact" not in handler_names


def test_tool_specs_have_one_category() -> None:
    for spec in TOOL_SPECS.values():
        assert isinstance(spec.category, ToolCategory)


def test_compact_tool_is_parent_only() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    assert "compact" in parent_tool_names
    assert "compact" not in child_tool_names
    assert TOOL_SPECS["compact"].handler is None


def run_all() -> None:
    test_list_ignores_internal_files()
    test_list_ignored_path_returns_hint()
    test_bash_blocks_dangerous_commands()
    test_workspace_tool()
    test_skill_loader()
    test_task_tool_registered()
    test_tool_schemas_match_handlers()
    test_tool_specs_have_one_category()
    test_compact_tool_is_parent_only()


if __name__ == "__main__":
    run_all()
    print("ok")
