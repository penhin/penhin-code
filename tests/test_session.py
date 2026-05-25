import sys
import tempfile
import contextlib

from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_module
import transcript


def test_parse_session_args() -> None:
    inspect_args = main_module.parse_args(["-i", "177909"])
    once_args = main_module.parse_args(["-o", "hello", "world"])

    assert inspect_args.inspect_session == "177909"
    assert once_args.once == ["hello", "world"]


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
    assert "--resume" in help_text
    assert "--once" in help_text


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


def run_all() -> None:
    test_parse_session_args()
    test_parse_help_command()
    test_load_initial_messages_new_session_flag()
    test_load_initial_messages_without_history()
    test_load_initial_messages_resumes_latest_transcript()
    test_load_initial_messages_resumes_specific_session()
    test_load_initial_session_returns_resumed_path()
    test_save_session_messages_updates_existing_session()


if __name__ == "__main__":
    run_all()
    print("ok")
