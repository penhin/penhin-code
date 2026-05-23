import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transcript

from tests.helpers import ToolUseBlock


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


def run_all() -> None:
    test_save_transcript_writes_jsonl()
    test_transcript_read_rejects_unsafe_paths()


if __name__ == "__main__":
    run_all()
    print("ok")
