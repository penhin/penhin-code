import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.tools.task_state import TaskStatusManager
from penhin.tools.builtin import tasks as task_tools

from tests.helpers import run_spec_tool


def test_todo_flow() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        manager.start("release package")
        set_result = manager.update_todos("set", ["inspect", "verify"])
        assert set_result.ok is True
        assert "1. [ ] inspect" in set_result.message
        assert "2. [ ] verify" in set_result.message

        done_result = manager.update_todos("done", index=1)
        assert done_result.ok is True
        assert "1. [x] inspect" in done_result.message

        show_result = manager.update_todos("show")
        assert show_result.ok is True
        assert "1. [x] inspect" in show_result.message

        clear_result = manager.update_todos("clear")
        assert clear_result.ok is True
        assert clear_result.message == "Cleared todos"


def test_todos_require_current_task() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = TaskStatusManager(Path(tmpdir)).update_todos("show")

    assert result.ok is False
    assert result.meta["code"] == "not_found"


def test_todo_tool_handlers() -> None:
    original_task_status = task_tools.task_status
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            task_tools.task_status.start("release package")
            set_result = run_spec_tool("todo_set", items=["inspect", "verify"])
            show_result = run_spec_tool("todo_show")
            done_result = run_spec_tool("todo_done", index=1)
            clear_result = run_spec_tool("todo_clear")

        assert set_result.ok is True
        assert show_result.ok is True
        assert "1. [ ] inspect" in show_result.message
        assert done_result.ok is True
        assert "1. [x] inspect" in done_result.message
        assert clear_result.ok is True
        assert clear_result.message == "Cleared todos"
    finally:
        task_tools.task_status = original_task_status
