import sys
import tempfile
import contextlib

import pytest

from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.cli import main as main_module
from penhin.agent import session_store
from penhin.agent import loop as loop_module


def test_parse_session_args() -> None:
    inspect_args = main_module.parse_args(["-i", "177909", "-e", "3"])
    once_args = main_module.parse_args(["-o", "hello", "world"])

    assert inspect_args.inspect_session == "177909"
    assert inspect_args.events == 3
    assert once_args.once == ["hello", "world"]
    assert main_module.parse_args(["--model", "gpt-4.1"]).model == "gpt-4.1"
    assert main_module.parse_args(["--provider", "openai"]).provider == "openai"
    with pytest.raises(SystemExit):
        main_module.parse_args(["--new"])


def test_interactive_skill_submission_loads_content_on_the_agent_queue(tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace
    from penhin.skills import SkillLoader
    from penhin.agent.session_manager import SessionManager
    from penhin.tools.registry import MODEL_TOOL_CATALOG

    skill = tmp_path / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Review changes\n---\nCheck correctness and tests.\n")
    manager = SessionManager.create(tmp_path / "sessions")
    completed, rejected = Event(), Event()
    received = []

    def run_agent(context):
        received.append(context.messages[-1]["content"])
        completed.set()

    class Terminal:
        def __init__(self, submit, **kwargs):
            self.submit = submit

        def run(self):
            self.submit("/skill:missing")
            assert rejected.wait(2)
            self.submit("/skill:review inspect this project")
            assert completed.wait(2)

    monkeypatch.setattr("penhin.cli.commands.router.load_skill", SkillLoader(tmp_path / "skills"))
    args = main_module.parse_args([])
    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "_session_for_args", lambda _: manager)
    monkeypatch.setattr(main_module.runtime_manager, "initialize", lambda **kwargs: None)
    monkeypatch.setattr(main_module.runtime_manager, "current", lambda: SimpleNamespace(model="test"))
    monkeypatch.setattr(main_module.runtime_manager, "available", lambda: False)
    monkeypatch.setattr(main_module.runtime_manager, "configured_provider", lambda: "test")
    monkeypatch.setattr(main_module, "get_permission_mode", lambda: "default")
    monkeypatch.setattr(main_module, "resolve_envelope", lambda *args: None)
    monkeypatch.setattr(main_module, "plugin_runtime_for_session", lambda: SimpleNamespace(
        catalog=lambda: MODEL_TOOL_CATALOG, active=lambda: (), close=lambda: None,
    ))
    monkeypatch.setattr(main_module, "workspace_info", lambda *args: {})
    monkeypatch.setattr(main_module, "agent_loop", run_agent)
    monkeypatch.setattr(main_module, "handle_local_command", lambda *args: pytest.fail("skill routed to local commands"))
    monkeypatch.setattr(main_module, "print_error", lambda _: rejected.set())
    monkeypatch.setattr(main_module, "print_welcome", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "print_user_message", lambda _: None)
    monkeypatch.setattr(main_module.ui, "TerminalInterface", Terminal)
    monkeypatch.setattr(main_module.ui, "activate_terminal", lambda *args: None)
    monkeypatch.setattr(main_module.ui, "deactivate_terminal", lambda: None)

    main_module.main()

    assert len(received) == 1
    assert "Check correctness and tests." in received[0]
    assert "inspect this project" in received[0]
    assert manager.build_context()[-1]["content"] == received[0]


@pytest.mark.parametrize("signal", [EOFError(), KeyboardInterrupt()])
def test_run_cli_exits_silently_for_terminal_exit_signals(monkeypatch, signal) -> None:
    monkeypatch.setattr(main_module, "main", lambda: (_ for _ in ()).throw(signal))

    assert main_module.run_cli() == 0


def test_parse_help_command() -> None:
    output = StringIO()

    with contextlib.redirect_stdout(output):
        try:
            main_module.parse_args(["help"])
            raise AssertionError("Expected help to exit")
        except SystemExit as error:
            assert error.code == 0

    help_text = output.getvalue()
    assert "--sessions" in help_text
    assert "--inspect-session" in help_text
    assert "--events" in help_text
    assert "--resume" in help_text
    assert "--once" in help_text


def test_workspace_summary_line() -> None:
    line = main_module.workspace_summary_line(
        {
            "git_branch": "main",
            "dirty_files_count": 2,
            "test_command_hint": ".venv/bin/python -m pytest -q",
            "has_agents_md": True,
        }
    )

    assert line == (
        "[workspace] branch=main "
        "dirty=2 "
        "test=.venv/bin/python -m pytest -q "
        "agents=true"
    )


def test_resume_without_history_creates_empty_session() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = session_store.SessionStore(Path(tmpdir))
        manager = store.resume()

        assert manager.build_context() == []
        assert manager.path.exists()


def test_resume_uses_latest_session() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = session_store.SessionStore(Path(tmpdir))
        messages = [{"role": "user", "content": "hello"}]
        created = store.new(messages)

        resumed = store.resume()
        assert resumed.path == created.path
        assert resumed.build_context() == messages


def test_resume_uses_specific_session() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = session_store.SessionStore(Path(tmpdir))
        first_messages = [{"role": "user", "content": "first"}]
        second_messages = [{"role": "user", "content": "second"}]
        first = store.new(first_messages)
        store.new(second_messages)

        session_ref = session_store.session_id_from_path(first.path)
        resumed = store.resume(session_ref)

        assert resumed.path == first.path
        assert resumed.build_context() == first_messages


def test_default_start_creates_a_new_session_and_resume_is_explicit(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = session_store.SessionStore(Path(tmpdir))
        monkeypatch.setattr(main_module, "sessions", store)

        first = main_module._session_for_args(main_module.parse_args([]))
        second = main_module._session_for_args(main_module.parse_args([]))
        resumed = main_module._session_for_args(main_module.parse_args(["--resume", first.id]))

        assert first.path != second.path
        assert resumed.path == first.path


def test_resume_of_a_missing_session_fails_clearly(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(main_module, "sessions", session_store.SessionStore(Path(tmpdir)))

        with pytest.raises(SystemExit, match="Session resume failed: Session not found"):
            main_module._session_for_args(main_module.parse_args(["--resume", "missing"]))


def test_once_appends_to_the_selected_session(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = session_store.SessionStore(Path(tmpdir)).new([
            {"role": "user", "content": "earlier"},
        ])
        monkeypatch.setattr(
            loop_module,
            "agent_loop",
            lambda context: context.add_assistant_message("done"),
        )

        loop_module.run_once_prompt("continue", manager)

        assert manager.build_context() == [
            {"role": "user", "content": "earlier"},
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "done"},
        ]


def test_print_session_list_marks_latest() -> None:
    original_sessions = main_module.sessions
    output = StringIO()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            store = session_store.SessionStore(Path(tmpdir))
            store.new([{"role": "user", "content": "first"}])
            latest = store.new([{"role": "user", "content": "latest"}])
            latest_id = session_store.session_id_from_path(latest.path)[:12]
            main_module.sessions = store

            with contextlib.redirect_stdout(output):
                main_module.print_session_list()
        finally:
            main_module.sessions = original_sessions

    lines = output.getvalue().splitlines()
    assert lines[0] == "mark | id | updated | msgs | request"
    marked_lines = [line for line in lines[1:] if line.startswith("* | ")]
    assert len(marked_lines) == 1
    assert latest_id in marked_lines[0]


def test_session_inspect_counts_tool_results() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = session_store.SessionStore(Path(tmpdir))
        messages = [
            {"role": "user", "content": "read a file"},
            {"role": "assistant", "content": "I will read it"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "tool_name": "read",
                        "content": '{"ok": true, "exit_code": 0}',
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-2",
                        "tool_name": "write",
                        "content": '{"ok": false, "exit_code": 1, "meta": {"code": "invalid_tool_input"}}',
                    },
                ],
            },
            {"role": "user", "content": "now summarize"},
            {"role": "assistant", "content": "done"},
        ]
        manager = store.new(messages)

        inspected = store.inspect(session_store.session_id_from_path(manager.path))

        assert inspected.first_user == "read a file"
        assert inspected.last_user == "now summarize"
        assert inspected.last_assistant == "done"
        assert inspected.tool_result_count == 2
        assert inspected.failed_tool_result_count == 1
        assert inspected.event_count == 6
        assert inspected.recent_events == [
            "user | read a file",
            "assistant | I will read it",
            "tool_result | ok | read | tool-1",
            "tool_result | error | write | tool-2 | invalid_tool_input",
            "user | now summarize",
            "assistant | done",
        ]

        limited = store.inspect(session_store.session_id_from_path(manager.path), event_limit=3)

        assert limited.event_count == 6
        assert limited.recent_events == [
            "tool_result | error | write | tool-2 | invalid_tool_input",
            "user | now summarize",
            "assistant | done",
        ]
