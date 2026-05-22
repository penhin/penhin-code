import io
import json
import logging
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import atomic_io
import compact
import tool_runtime
import transcript
import tools
from compact import (
    compact_source_text,
    auto_compact_messages,
    micro_compact_text,
    recent_message_start,
    should_auto_compact,
)
from result import Result
from skills import load_skill
from task import TaskStatusManager
from todo import TODO_FILE, run_todo
from tool_runtime import ApprovalFlow, CHILD_AGENT_POLICY, PARENT_AGENT_POLICY, PermissionPolicy, ToolRun, run_tool
from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_SPECS, ToolCategory, run_list


def run_spec_tool(tool_name: str, **kwargs) -> Result:
    handler = TOOL_SPECS[tool_name].handler
    assert handler is not None
    return handler(**kwargs)


class ToolUseBlock:
    def __init__(self, block_id: str, name: str):
        self.type = "tool_use"
        self.id = block_id
        self.name = name


def test_result_json() -> None:
    data = json.loads(Result.success("hello", data={"value": 1}).to_json())
    assert data["ok"] is True
    assert data["message"] == "hello"
    assert data["data"] == {"value": 1}
    assert data["error"] == ""
    assert data["exit_code"] == 0
    assert data["stdout"] == "hello"
    assert data["stderr"] == ""

    failed = Result.failure("broken", code="test_error")
    failed_data = json.loads(failed.to_json())
    assert failed.exit_code == 1
    assert failed.stderr == "broken"
    assert failed_data["ok"] is False
    assert failed_data["meta"]["code"] == "test_error"


def test_result_summary_reports_sizes_without_content() -> None:
    result = Result.success("secret output", data={"value": 1}, cached=True)
    summary = result.summary()

    assert summary == {
        "stdout_chars": len("secret output"),
        "stderr_chars": 0,
        "data_type": "dict",
        "meta_keys": ["cached"],
    }
    assert "secret output" not in json.dumps(summary)


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


def test_atomic_write_cleans_temp_file_on_replace_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "target.txt"
        temp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            try:
                atomic_io.atomic_write_text(path, "content")
            except OSError:
                pass
            else:
                raise AssertionError("expected replace failure")

        assert not temp_path.exists()
        assert not path.exists()


def test_atomic_json_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "data.json"
        data = {"name": "penhin", "items": [1, 2]}

        atomic_io.write_json_atomic(path, data)

        assert atomic_io.read_json(path) == data


def test_atomic_jsonl_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "data.jsonl"
        items = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

        atomic_io.write_jsonl_atomic(path, items)

        assert atomic_io.read_jsonl(path) == items


def test_todo_flow() -> None:
    original = TODO_FILE.read_text(encoding="utf-8") if TODO_FILE.exists() else None
    try:
        set_result = run_todo("set", ["inspect", "verify"])
        assert set_result.exit_code == 0
        assert "1. [ ] inspect" in set_result.stdout
        assert "2. [ ] verify" in set_result.stdout

        done_result = run_todo("done", index=1)
        assert done_result.exit_code == 0
        assert "1. [x] inspect" in done_result.stdout

        show_result = run_todo("show")
        assert show_result.exit_code == 0
        assert "1. [x] inspect" in show_result.stdout

        clear_result = run_todo("clear")
        assert clear_result.exit_code == 0
        assert clear_result.stdout == "Cleared todos"
    finally:
        if original is None:
            TODO_FILE.unlink(missing_ok=True)
        else:
            TODO_FILE.write_text(original, encoding="utf-8")


def test_todo_tool_handlers() -> None:
    original = TODO_FILE.read_text(encoding="utf-8") if TODO_FILE.exists() else None
    try:
        set_result = run_spec_tool("todo_set", items=["inspect", "verify"])
        show_result = run_spec_tool("todo_show")
        done_result = run_spec_tool("todo_done", index=1)
        clear_result = run_spec_tool("todo_clear")

        assert set_result.exit_code == 0
        assert show_result.exit_code == 0
        assert "1. [ ] inspect" in show_result.stdout
        assert done_result.exit_code == 0
        assert "1. [x] inspect" in done_result.stdout
        assert clear_result.exit_code == 0
        assert clear_result.stdout == "Cleared todos"
    finally:
        if original is None:
            TODO_FILE.unlink(missing_ok=True)
        else:
            TODO_FILE.write_text(original, encoding="utf-8")


def test_workspace_tool() -> None:
    result = run_spec_tool("workspace")
    assert result.exit_code == 0

    data = json.loads(result.stdout)
    result_data = json.loads(result.to_json())["data"]
    assert "cwd" in data
    assert ".venv" in data["ignored"]
    assert ".penhin_todos.json" in data["ignored"]
    assert "workspace" in data["tools"]
    assert "task" in data["tools"]
    assert result_data["cwd"] == data["cwd"]


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


def test_tool_runtime_policy_and_control_signals() -> None:
    assert "compact" in PARENT_AGENT_POLICY.allow
    assert "task" in PARENT_AGENT_POLICY.allow
    assert "task" not in CHILD_AGENT_POLICY.allow

    approval = ApprovalFlow.preapproved({"workspace", "compact"})

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny={"workspace"})
    denied = run_tool("workspace", {}, policy)
    assert denied.result.exit_code == 1
    assert "Denied by policy" in denied.result.stderr

    policy = PermissionPolicy(allow={"workspace", "compact"}, deny=set())
    workspace = run_tool("workspace", {}, policy)
    assert workspace.result.exit_code == 0
    assert workspace.manual_compact is False

    compact_run = run_tool("compact", {}, policy, approval)
    assert compact_run.result.exit_code == 0
    assert compact_run.manual_compact is True

    not_allowed = run_tool("read", {"path": "README.md"}, policy)
    assert not_allowed.result.exit_code == 1
    assert "Not allowed by policy" in not_allowed.result.stderr

    approval_required = run_tool(
        "write",
        {"path": "README.md", "content": "test"},
        PermissionPolicy(allow={"write"}, deny=set()),
        ApprovalFlow.require_confirmation({"write"}),
    )
    assert approval_required.result.exit_code == 1
    assert "Approval required" in approval_required.result.stderr


def test_tool_runtime_input_summary_hides_sensitive_values() -> None:
    summary = tool_runtime.input_summary(
        {
            "path": "agent.py",
            "content": "secret content",
            "command": "echo secret",
            "unknown": "hidden",
        }
    )

    assert "path:agent.py" in summary
    assert "content:sha256:" in summary
    assert "command:sha256:" in summary
    assert "unknown:<hidden>" in summary
    assert "secret content" not in summary
    assert "echo secret" not in summary


def test_tool_runtime_logs_result_status() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("penhin.tool")
    original_level = logger.level
    original_propagate = logger.propagate

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        with patch.object(
            tool_runtime,
            "execute_tool",
            return_value=ToolRun(Result.failure("broken", code="tool_error")),
        ):
            result = run_tool("workspace", {}, PermissionPolicy(allow={"workspace"}, deny=set()))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    output = stream.getvalue()
    assert result.result.exit_code == 1
    assert "[tool] start call_id=tool-" in output
    assert "name=workspace input=<none>" in output
    assert "status=error" in output
    assert "duration_ms=" in output
    assert "code=tool_error" in output
    assert "stdout_chars=0" in output
    assert "stderr_chars=6" in output
    assert 'data_type="none"' in output
    assert 'meta_keys=["code"]' in output


def test_tool_runtime_logs_input_summary() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("penhin.tool")
    original_level = logger.level
    original_propagate = logger.propagate

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        with patch.object(
            tool_runtime,
            "execute_tool",
            return_value=ToolRun(Result.success("ok")),
        ):
            result = run_tool(
                "write",
                {"path": "agent.py", "content": "secret content"},
                PermissionPolicy(allow={"write"}, deny=set()),
                ApprovalFlow.preapproved({"write"}),
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    output = stream.getvalue()
    assert result.result.exit_code == 0
    assert "[tool] start call_id=tool-" in output
    assert "input=content:sha256:" in output
    assert "path:agent.py" in output
    assert "secret content" not in output


def test_tool_runtime_logs_blocked_access() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("penhin.tool")
    original_level = logger.level
    original_propagate = logger.propagate

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        result = run_tool(
            "write",
            {"path": "agent.py", "content": "secret content"},
            PermissionPolicy(allow={"write"}, deny=set()),
            ApprovalFlow.require_confirmation({"write"}),
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    output = stream.getvalue()
    assert result.approval_required is True
    assert result.result.exit_code == 1
    assert "[tool] blocked call_id=tool-" in output
    assert "name=write" in output
    assert "code=tool_approval_required" in output
    assert "path:agent.py" in output
    assert "content:sha256:" in output
    assert "secret content" not in output


def test_resolve_approval_approves_for_session() -> None:
    approval = ApprovalFlow.require_confirmation({"write"})
    tool_input = {"path": "demo.txt", "content": "hello"}

    with patch("builtins.input", return_value="ys"), patch("agent.run_tool") as mocked_run_tool:
        mocked_run_tool.return_value = ToolRun(Result.success("ok"))
        tool_run = agent.resolve_approval("write", tool_input, approval)

    assert tool_run.result.stdout == "ok"
    assert approval.is_approved("write", tool_input)
    mocked_run_tool.assert_called_once()


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


def test_micro_compact_text() -> None:
    long_output = "x" * 200
    preserved_output = "y" * 200
    recent_output = "z" * 200

    messages = [
        {
            "role": "assistant",
            "content": [
                ToolUseBlock("tool-1", "list"),
                ToolUseBlock("tool-2", "read"),
                ToolUseBlock("tool-3", "bash"),
                ToolUseBlock("tool-4", "search"),
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": long_output},
                {"type": "tool_result", "tool_use_id": "tool-2", "content": preserved_output},
                {"type": "tool_result", "tool_use_id": "tool-3", "content": recent_output},
                {"type": "tool_result", "tool_use_id": "tool-4", "content": recent_output},
            ],
        },
    ]

    micro_compact_text(messages, keep_recent=2)

    results = messages[1]["content"]
    assert results[0]["content"] == "[Previous: used list]"
    assert results[1]["content"] == preserved_output
    assert results[2]["content"] == recent_output
    assert results[3]["content"] == recent_output

    todo_messages = [
        {"role": "assistant", "content": [ToolUseBlock("tool-5", "todo_show")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-5", "content": long_output}],
        },
    ]
    micro_compact_text(todo_messages, keep_recent=0)
    assert todo_messages[1]["content"][0]["content"] == long_output


def test_auto_compact_helpers() -> None:
    assert should_auto_compact([{"role": "user", "content": "x" * 40}], threshold=5)
    assert not should_auto_compact([{"role": "user", "content": "short"}], threshold=100)

    messages = [
        {"role": "user", "content": "initial"},
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}],
        },
        {"role": "assistant", "content": "done"},
    ]
    assert recent_message_start(messages, keep_last=2) == 1


def test_compact_source_text_keeps_head_and_tail() -> None:
    original_head_chars = compact.SUMMARY_HEAD_CHARS
    original_tail_chars = compact.SUMMARY_TAIL_CHARS

    try:
        compact.SUMMARY_HEAD_CHARS = 80
        compact.SUMMARY_TAIL_CHARS = 80

        messages = [
            {"role": "user", "content": "HEAD-" + ("x" * 200) + "-TAIL"},
        ]
        text = compact_source_text(messages)
    finally:
        compact.SUMMARY_HEAD_CHARS = original_head_chars
        compact.SUMMARY_TAIL_CHARS = original_tail_chars

    assert "HEAD-" in text
    assert "...[middle omitted during compaction]..." in text
    assert "-TAIL" in text


def test_save_transcript_writes_jsonl() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        ]
        transcript_path = store.save(messages)

        assert transcript_path.parent == Path(tmpdir)
        assert transcript_path.exists()
        assert store.latest() == transcript_path
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
        stored_messages = store.read(transcript_path)

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"role": "user", "content": "hello"}
    assert json.loads(lines[1])["content"][0]["type"] == "ToolUseBlock"
    assert stored_messages[0] == {"role": "user", "content": "hello"}


def test_transcript_read_rejects_unsafe_paths() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir) / "transcripts")
        transcript_path = store.save([{"role": "user", "content": "hello"}])
        wrong_suffix = Path(tmpdir) / "transcripts" / "notes.txt"
        outside_path = Path(tmpdir) / "outside.jsonl"
        wrong_suffix.write_text("{}", encoding="utf-8")
        outside_path.write_text("{}", encoding="utf-8")

        assert store.read(transcript_path) == [{"role": "user", "content": "hello"}]

        try:
            store.read(wrong_suffix)
            raise AssertionError("Expected wrong suffix to be rejected")
        except ValueError as error:
            assert ".jsonl" in str(error)

        try:
            store.read(outside_path)
            raise AssertionError("Expected outside path to be rejected")
        except ValueError as error:
            assert "escapes transcript directory" in str(error)


def test_auto_compact_falls_back_when_summary_fails() -> None:
    class FailingRuntime:
        def call_llm_once(self, **kwargs):
            raise RuntimeError("offline failure")

    original_get_runtime = compact.get_runtime
    original_transcripts = compact.transcripts

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            compact.get_runtime = lambda: FailingRuntime()
            compact.transcripts = transcript.TranscriptStore(Path(tmpdir))

            messages = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
            compacted = auto_compact_messages(messages, keep_last=1)
        finally:
            compact.get_runtime = original_get_runtime
            compact.transcripts = original_transcripts

    assert compacted[0]["role"] == "user"
    assert str(Path(tmpdir)) in compacted[0]["content"]
    assert "Summary failed during compaction: offline failure" in compacted[0]["content"]
    assert compacted[-1] == {"role": "assistant", "content": "second"}


def main() -> None:
    test_result_json()
    test_result_summary_reports_sizes_without_content()
    test_list_ignores_internal_files()
    test_list_ignored_path_returns_hint()
    test_bash_blocks_dangerous_commands()
    test_atomic_write_cleans_temp_file_on_replace_failure()
    test_atomic_json_round_trip()
    test_atomic_jsonl_round_trip()
    test_todo_flow()
    test_todo_tool_handlers()
    test_workspace_tool()
    test_skill_loader()
    test_task_tool_registered()
    test_tool_schemas_match_handlers()
    test_tool_specs_have_one_category()
    test_compact_tool_is_parent_only()
    test_tool_runtime_policy_and_control_signals()
    test_tool_runtime_input_summary_hides_sensitive_values()
    test_tool_runtime_logs_result_status()
    test_tool_runtime_logs_input_summary()
    test_tool_runtime_logs_blocked_access()
    test_task_status_tool_registered()
    test_task_status_manager_flow()
    test_task_status_write_cleans_temp_file_on_failure()
    test_task_status_current_write_cleans_temp_file_on_failure()
    test_task_status_manager_list_and_switch()
    test_background_task_manager_flow()
    test_background_start_rejects_nested_background_tasks()
    test_background_start_uses_daemon_thread()
    test_task_status_tool_handler()
    test_micro_compact_text()
    test_auto_compact_helpers()
    test_compact_source_text_keeps_head_and_tail()
    test_save_transcript_writes_jsonl()
    test_transcript_read_rejects_unsafe_paths()
    test_auto_compact_falls_back_when_summary_fails()
    print("ok")


if __name__ == "__main__":
    main()
