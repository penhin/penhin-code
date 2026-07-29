import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from task import TaskStatusManager
from orchestration.models import AgentJob, AgentRole
from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS
from tools import tasks as task_tools

from tests.helpers import run_spec_tool


def test_task_status_tool_registered() -> None:
    parent_tool_names = {tool["name"] for tool in PARENT_TOOLS}
    child_tool_names = {tool["name"] for tool in CHILD_TOOLS}

    for tool_name in {
        "task_start",
        "task_show",
        "task_complete",
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
        started = manager.start(
            "build task system",
            description="track task state",
            note="initial",
            plan_slug="calm-bright-plan",
        )
        manager.mark_plan_verified(started.id, "calm-bright-plan")
        shown = manager.show()
        completed = manager.complete(note="done")
        no_current = manager.show()

    assert started.id == 1
    assert started.status == "running"
    assert started.plan_slug == "calm-bright-plan"
    assert shown.subject == "build task system"
    assert shown.plan_slug == "calm-bright-plan"
    assert shown.verified_plan_slug == "calm-bright-plan"
    assert completed.status == "completed"
    assert completed.note == "done"
    assert no_current is None


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


def test_background_task_manager_flow() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TaskStatusManager(Path(tmpdir))
        main_task = manager.start("main task")
        background = manager.start_background("inspect parser")
        completed = manager.finish_background(background.id, "completed", result="parser summary")
        listed = json.loads(manager("background_list").message)
        shown = json.loads(manager("background_show", id=background.id).message)
        main_show = manager("background_show", id=main_task.id)

    assert background.kind == "background"
    assert completed.status == "completed"
    assert completed.result == "parser summary"
    assert [task["id"] for task in listed] == [background.id]
    assert "result" not in listed[0]
    assert shown["kind"] == "background"
    assert shown["result"] == "parser summary"
    assert main_show.ok is False
    assert "Background task" in main_show.error


def test_background_start_rejects_nested_background_tasks() -> None:
    results = []
    thread = threading.Thread(
        target=lambda: results.append(task_tools.run_background_start("nested")),
        name="background-task-test",
    )
    thread.start()
    thread.join()

    assert results[0].ok is False
    assert "cannot start nested background tasks" in results[0].error


def test_background_start_uses_daemon_thread() -> None:
    original_task_status = task_tools.task_status
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
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            fake_job = AgentJob(
                id="test-background-job",
                root_task_id="test-background-job",
                role=AgentRole.GENERAL,
                subject="summarize files",
                instruction="summarize files",
                workspace_mode="isolated_write",
                worktree_path="/tmp/test-background-worktree",
                worktree_branch="penhin/test-background",
            )
            with patch.object(task_tools.threading, "Thread", FakeThread), patch(
                "orchestration.service.enqueue_subagent_job", return_value=fake_job,
            ):
                result = task_tools.run_background_start("summarize files")
        finally:
            task_tools.task_status = original_task_status

    assert result.ok is True
    assert len(created_threads) == 1
    assert created_threads[0].daemon is True
    assert created_threads[0].started is True
    assert created_threads[0].name.startswith("background-task-")


def test_task_status_tool_handler() -> None:
    original_task_status = task_tools.task_status

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            start_result = run_spec_tool(
                "task_start",
                subject="wire task_status",
                description="Expose task status to the parent agent",
                plan_slug="quiet-swift-bold",
            )
            started = json.loads(start_result.message)
            complete_first_result = run_spec_tool("task_complete", note="ready for next task")
            second_result = run_spec_tool("task_start", subject="second task")
            second = json.loads(second_result.message)

            show_result = run_spec_tool("task_show", id=started["id"])
            shown = json.loads(show_result.message)
        finally:
            task_tools.task_status = original_task_status

    assert start_result.ok is True
    assert started["subject"] == "wire task_status"
    assert started["plan_slug"] == "quiet-swift-bold"
    assert json.loads(start_result.to_json())["data"]["subject"] == "wire task_status"
    assert complete_first_result.ok is True
    assert second_result.ok is True
    assert second["subject"] == "second task"
    assert show_result.ok is True
    assert shown["id"] == started["id"]
    assert shown["plan_slug"] == "quiet-swift-bold"


def test_task_complete_reports_unverified_plan() -> None:
    original_task_status = task_tools.task_status

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            run_spec_tool(
                "task_start",
                subject="planned work",
                plan_slug="unverified-plan",
            )
            result = run_spec_tool("task_complete", note="done")
        finally:
            task_tools.task_status = original_task_status

    assert result.ok is True
    assert result.data["plan_slug"] == "unverified-plan"
    assert result.data["unverified_plan"] is True


def test_task_complete_reports_verified_plan() -> None:
    original_task_status = task_tools.task_status

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            started = task_tools.task_status.start("planned work", plan_slug="verified-plan")
            task_tools.task_status.mark_plan_verified(started.id, "verified-plan")
            result = run_spec_tool("task_complete", note="done")
        finally:
            task_tools.task_status = original_task_status

    assert result.ok is True
    assert result.data["plan_slug"] == "verified-plan"
    assert result.data["verified_plan_slug"] == "verified-plan"
    assert result.data["unverified_plan"] is False


def run_all() -> None:
    test_task_status_tool_registered()
    test_task_status_manager_flow()
    test_task_status_write_cleans_temp_file_on_failure()
    test_task_status_current_write_cleans_temp_file_on_failure()
    test_background_task_manager_flow()
    test_background_start_rejects_nested_background_tasks()
    test_background_start_uses_daemon_thread()
    test_task_status_tool_handler()
    test_task_complete_reports_unverified_plan()
    test_task_complete_reports_verified_plan()


if __name__ == "__main__":
    run_all()
    print("ok")
