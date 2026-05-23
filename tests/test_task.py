import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools
from task import TaskStatusManager
from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS

from tests.helpers import run_spec_tool


def test_task_status_tool_registered() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    for tool_name in {
        "task_start",
        "task_show",
        "task_complete",
        "task_block",
        "task_clear",
        "task_list",
        "task_switch",
        "background_start",
        "background_list",
        "background_show",
    }:
        assert tool_name in parent_tool_names
        assert tool_name not in child_tool_names
        assert TOOL_SPECS[tool_name].handler is not None


def test_task_status_manager_flow() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        started = manager.start("build task system", description="track task state", note="initial")
        shown = manager.show()
        blocked = manager.block(blocked_by=[99], note="waiting")
        completed = manager.complete(note="done")
        manager.clear()
        cleared = manager.show()

    assert started.id == 1
    assert started.status == "running"
    assert shown.subject == "build task system"
    assert blocked.status == "blocked"
    assert blocked.blocked_by == [99]
    assert blocked.note == "waiting"
    assert blocked.updated_at >= blocked.created_at
    assert completed.status == "completed"
    assert completed.blocked_by == []
    assert completed.note == "done"
    assert cleared is None


def test_task_status_write_cleans_temp_file_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        task = manager.start("build task system")
        path = manager._task_path(task.id)
        temp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        task.note = "updated"

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            try:
                manager._save(task)
            except OSError:
                pass
            else:
                raise AssertionError("expected replace failure")

        assert not temp_path.exists()


def test_task_status_current_write_cleans_temp_file_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        temp_path = manager.current_file.with_name(f".current.json.{threading.get_ident()}.tmp")

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            try:
                manager._set_current_id(1)
            except OSError:
                pass
            else:
                raise AssertionError("expected replace failure")

        assert not temp_path.exists()


def test_task_status_manager_list_and_switch() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        first = manager.start("first task")
        second = manager.start("second task")
        listed = manager.list()
        shown_first = manager.show(first.id)
        switched = manager.switch(first.id)
        current = manager.show()
        missing_switch = manager("switch", id=99)

    assert [task["id"] for task in listed] == [first.id, second.id]
    assert shown_first.subject == "first task"
    assert switched.id == first.id
    assert current.id == first.id
    assert missing_switch.exit_code == 1
    assert "Task 99 not found" in missing_switch.stderr


def test_background_task_manager_flow() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        main_task = manager.start("main task")
        background = manager.start_background("inspect parser")
        completed = manager.finish_background(background.id, "completed", result="parser summary")
        listed = json.loads(manager("background_list").stdout)
        shown = json.loads(manager("background_show", id=background.id).stdout)
        main_show = manager("background_show", id=main_task.id)

    assert background.kind == "background"
    assert completed.status == "completed"
    assert completed.result == "parser summary"
    assert [task["id"] for task in listed] == [background.id]
    assert "result" not in listed[0]
    assert shown["kind"] == "background"
    assert shown["result"] == "parser summary"
    assert main_show.exit_code == 1
    assert "Background task" in main_show.stderr


def test_background_start_rejects_nested_background_tasks() -> None:
    results = []
    thread = threading.Thread(
        target=lambda: results.append(tools.run_background_start("nested")),
        name="background-task-test",
    )
    thread.start()
    thread.join()

    assert results[0].exit_code == 1
    assert "cannot start nested background tasks" in results[0].stderr


def test_background_start_uses_daemon_thread() -> None:
    original_task_status = tools.task_status
    created_threads = []

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name
            self.started = False
            created_threads.append(self)

        def start(self):
            self.started = True

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tools.task_status = TaskStatusManager(Path(tmpdir))
            with patch.object(tools.threading, "Thread", FakeThread):
                result = tools.run_background_start("summarize files")
        finally:
            tools.task_status = original_task_status

    assert result.exit_code == 0
    assert len(created_threads) == 1
    assert created_threads[0].daemon is True
    assert created_threads[0].started is True
    assert created_threads[0].name.startswith("background-task-")


def test_task_status_tool_handler() -> None:
    original_task_status = tools.task_status

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tools.task_status = TaskStatusManager(Path(tmpdir))
            start_result = run_spec_tool(
                "task_start",
                subject="wire task_status",
                description="Expose task status to the parent agent",
            )
            started = json.loads(start_result.stdout)
            second_result = run_spec_tool("task_start", subject="second task")
            second = json.loads(second_result.stdout)

            block_result = run_spec_tool(
                "task_block",
                blocked_by=[99],
            )
            blocked = json.loads(block_result.stdout)

            show_result = run_spec_tool("task_show", id=started["id"])
            shown = json.loads(show_result.stdout)
            list_result = run_spec_tool("task_list")
            listed = json.loads(list_result.stdout)
            switch_result = run_spec_tool("task_switch", id=started["id"])
            switched = json.loads(switch_result.stdout)

            clear_result = run_spec_tool("task_clear")
            empty_result = run_spec_tool("task_show")
        finally:
            tools.task_status = original_task_status

    assert start_result.exit_code == 0
    assert started["subject"] == "wire task_status"
    assert json.loads(start_result.to_json())["data"]["subject"] == "wire task_status"
    assert second_result.exit_code == 0
    assert second["subject"] == "second task"
    assert block_result.exit_code == 0
    assert blocked["status"] == "blocked"
    assert blocked["blocked_by"] == [99]
    assert show_result.exit_code == 0
    assert shown["id"] == started["id"]
    assert list_result.exit_code == 0
    assert [task["id"] for task in listed] == [started["id"], second["id"]]
    assert json.loads(list_result.to_json())["data"][0]["id"] == started["id"]
    assert switch_result.exit_code == 0
    assert switched["id"] == started["id"]
    assert clear_result.stdout == "Cleared current task"
    assert empty_result.stdout == "(no current task)"


def run_all() -> None:
    test_task_status_tool_registered()
    test_task_status_manager_flow()
    test_task_status_write_cleans_temp_file_on_failure()
    test_task_status_current_write_cleans_temp_file_on_failure()
    test_task_status_manager_list_and_switch()
    test_background_task_manager_flow()
    test_background_start_rejects_nested_background_tasks()
    test_background_start_uses_daemon_thread()
    test_task_status_tool_handler()


if __name__ == "__main__":
    run_all()
    print("ok")
