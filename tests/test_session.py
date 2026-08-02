import sys
import tempfile
import contextlib

import pytest

from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.cli import main as main_module
from penhin.agent import transcript


def test_parse_session_args() -> None:
    inspect_args = main_module.parse_args(["-i", "177909", "-e", "3"])
    once_args = main_module.parse_args(["-o", "hello", "world"])

    assert inspect_args.inspect_session == "177909"
    assert inspect_args.events == 3
    assert once_args.once == ["hello", "world"]
    assert main_module.parse_args(["--model", "gpt-4.1"]).model == "gpt-4.1"
    assert main_module.parse_args(["--provider", "openai"]).provider == "openai"


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


def test_load_initial_messages_new_session_flag() -> None:
    store = transcript.TranscriptStore(Path(tempfile.mkdtemp()))

    assert store.load_session(resume=False) == ([], None)


def test_load_initial_messages_without_history() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))

        assert store.load_session(resume=True) == ([], None)


def test_load_initial_messages_resumes_latest_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
        messages = [{"role": "user", "content": "hello"}]
        session_path = store.save(messages)

        assert store.load_session(resume=True) == (messages, session_path)


def test_load_initial_messages_resumes_specific_session() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
        first_messages = [{"role": "user", "content": "first"}]
        second_messages = [{"role": "user", "content": "second"}]
        first_path = store.save(first_messages)
        store.save(second_messages)

        session_ref = transcript.session_id_from_path(first_path)

        assert store.load_session(resume=True, session_ref=session_ref) == (first_messages, first_path)


def test_load_initial_session_returns_resumed_path() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
        messages = [{"role": "user", "content": "hello"}]
        session_path = store.save(messages)

        loaded_messages, loaded_path = store.load_session(resume=True)

        assert loaded_messages == messages
        assert loaded_path == session_path


def test_save_session_messages_updates_existing_session() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
        original_messages = [{"role": "user", "content": "first"}]
        updated_messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "done"},
        ]
        session_path = store.save(original_messages)

        saved_path = store.save_session(session_path, updated_messages)

        assert saved_path == session_path
        assert store.read(session_path) == updated_messages
        assert len(list(Path(tmpdir).glob("transcript_*.jsonl"))) == 1


def test_print_session_list_marks_latest() -> None:
    original_transcripts = main_module.transcripts
    output = StringIO()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            store = transcript.TranscriptStore(Path(tmpdir))
            store.save([{"role": "user", "content": "first"}])
            latest_path = store.save([{"role": "user", "content": "latest"}])
            latest_id = transcript.session_id_from_path(latest_path)[:12]
            main_module.transcripts = store

            with contextlib.redirect_stdout(output):
                main_module.print_session_list()
        finally:
            main_module.transcripts = original_transcripts

    lines = output.getvalue().splitlines()
    assert lines[0] == "mark | id | updated | msgs | request"
    marked_lines = [line for line in lines[1:] if line.startswith("* | ")]
    assert len(marked_lines) == 1
    assert latest_id in marked_lines[0]


def test_session_inspect_counts_tool_results() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = transcript.TranscriptStore(Path(tmpdir))
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
        session_path = store.save(messages)

        inspected = store.inspect(transcript.session_id_from_path(session_path))

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

        limited = store.inspect(transcript.session_id_from_path(session_path), event_limit=3)

        assert limited.event_count == 6
        assert limited.recent_events == [
            "tool_result | error | write | tool-2 | invalid_tool_input",
            "user | now summarize",
            "assistant | done",
        ]
