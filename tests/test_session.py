import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_module
import transcript


def test_parse_session_args() -> None:
    inspect_args = main_module.parse_args(["-i", "177909"])
    once_args = main_module.parse_args(["-o", "hello", "world"])

    assert inspect_args.inspect_session == "177909"
    assert once_args.once == ["hello", "world"]


def test_load_initial_messages_new_session_flag() -> None:
    assert main_module.load_initial_messages(resume=False) == []


def test_load_initial_messages_without_history() -> None:
    original_transcripts = main_module.transcripts
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            main_module.transcripts = transcript.TranscriptStore(Path(tmpdir))
            assert main_module.load_initial_messages(resume=True) == []
        finally:
            main_module.transcripts = original_transcripts


def test_load_initial_messages_resumes_latest_transcript() -> None:
    original_transcripts = main_module.transcripts
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            store = transcript.TranscriptStore(Path(tmpdir))
            messages = [{"role": "user", "content": "hello"}]
            store.save(messages)
            main_module.transcripts = store

            assert main_module.load_initial_messages(resume=True) == messages
        finally:
            main_module.transcripts = original_transcripts


def test_load_initial_messages_resumes_specific_session() -> None:
    original_transcripts = main_module.transcripts
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            store = transcript.TranscriptStore(Path(tmpdir))
            first_messages = [{"role": "user", "content": "first"}]
            second_messages = [{"role": "user", "content": "second"}]
            first_path = store.save(first_messages)
            store.save(second_messages)
            main_module.transcripts = store

            session_ref = transcript.session_id_from_path(first_path)

            assert main_module.load_initial_messages(resume=True, session_ref=session_ref) == first_messages
        finally:
            main_module.transcripts = original_transcripts


def run_all() -> None:
    test_parse_session_args()
    test_load_initial_messages_new_session_flag()
    test_load_initial_messages_without_history()
    test_load_initial_messages_resumes_latest_transcript()
    test_load_initial_messages_resumes_specific_session()


if __name__ == "__main__":
    run_all()
    print("ok")
