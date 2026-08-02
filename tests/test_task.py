import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from task import TaskStatus, TaskStatusManager
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
        "agent_job_start",
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
            todos=["inspect", "verify"],
        )
        manager.update_todos("done", index=1)
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
    assert completed.todos == [
        {"text": "inspect", "done": True},
        {"text": "verify", "done": False},
    ]
    assert no_current is None


def test_old_task_without_todos_loads_with_empty_list() -> None:
    restored = TaskStatus.from_dict({
        "id": 1,
        "subject": "legacy task",
        "kind": "main",
        "blocked_by": [2],
        "error": "old error",
        "result": "old result",
    })

    assert restored.todos == []


def test_legacy_background_record_is_not_exposed(tmp_path: Path) -> None:
    manager = TaskStatusManager(tmp_path)
    manager._task_path(7).write_text('{"id": 7, "subject": "old", "kind": "background"}', encoding="utf-8")

    result = manager("show", id=7)

    assert result.ok is False
    assert result.meta["code"] == "not_found"


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


def test_task_show_uses_requested_tasks_todos() -> None:
    original_task_status = task_tools.task_status

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            task_tools.task_status = TaskStatusManager(Path(tmpdir))
            first = task_tools.task_status.start("first", todos=["first todo"])
            task_tools.task_status.complete()
            task_tools.task_status.start("second", todos=["second todo"])

            result = task_tools.run_task_show(first.id)
        finally:
            task_tools.task_status = original_task_status

    assert result.data["todos"] == [
        {"index": 1, "text": "first todo", "done": False},
    ]
