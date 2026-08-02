import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transcript

from tests.helpers import ToolUseBlock


def test_tool_result_failure_uses_current_result_schema_only() -> None:
    assert transcript.tool_result_failed({"content": '{"ok": false, "error": "failed"}'}) is True
    assert transcript.tool_result_failed({"content": '{"exit_code": 1}'}) is False


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


def test_save_transcript_redacts_registered_secrets(tmp_path: Path) -> None:
    from auth.secrets import register_secret

    register_secret("transcript-secret-sentinel")
    path = transcript.TranscriptStore(tmp_path).save([
        {"role": "user", "content": "value=transcript-secret-sentinel"},
    ])

    content = path.read_text(encoding="utf-8")
    assert "transcript-secret-sentinel" not in content
    assert "<redacted>" in content


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
