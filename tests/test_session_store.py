import json
from pathlib import Path

from penhin.agent import session_store

from tests.helpers import ToolUseBlock


def test_tool_result_failure_uses_current_result_schema_only() -> None:
    assert session_store.tool_result_failed({"content": '{"ok": false, "error": "failed"}'}) is True
    assert session_store.tool_result_failed({"content": '{"exit_code": 1}'}) is False


def test_new_session_writes_header_and_message_entries(tmp_path: Path) -> None:
    store = session_store.SessionStore(tmp_path)
    manager = store.new([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
    ])

    lines = manager.path.read_text(encoding="utf-8").splitlines()
    header, first, second = map(json.loads, lines)

    assert manager.path.parent == tmp_path
    assert manager.path.name.startswith("session_")
    assert store.latest() == manager.path
    assert header["type"] == "session"
    assert header["version"] == 1
    assert first["type"] == "message"
    assert first["parentId"] is None
    assert first["message"] == {"role": "user", "content": "hello"}
    assert second["parentId"] == first["id"]
    assert second["message"]["content"][0]["type"] == "ToolUseBlock"
    assert manager.build_context()[0] == {"role": "user", "content": "hello"}


def test_session_store_redacts_registered_secrets(tmp_path: Path) -> None:
    from penhin.auth.secrets import register_secret

    register_secret("session-secret-sentinel")
    manager = session_store.SessionStore(tmp_path).new([
        {"role": "user", "content": "value=session-secret-sentinel"},
    ])

    content = manager.path.read_text(encoding="utf-8")
    assert "session-secret-sentinel" not in content
    assert "<redacted>" in content


def test_session_store_rejects_unsafe_paths(tmp_path: Path) -> None:
    store = session_store.SessionStore(tmp_path / "sessions")
    manager = store.new([{"role": "user", "content": "hello"}])
    wrong_suffix = tmp_path / "sessions" / "notes.txt"
    outside_path = tmp_path / "outside.jsonl"
    wrong_suffix.write_text("{}", encoding="utf-8")
    outside_path.write_text("{}", encoding="utf-8")

    assert store.open(manager.path).build_context() == [{"role": "user", "content": "hello"}]

    try:
        store.open(wrong_suffix)
        raise AssertionError("Expected wrong suffix to be rejected")
    except ValueError as error:
        assert ".jsonl" in str(error)

    try:
        store.open(outside_path)
        raise AssertionError("Expected outside path to be rejected")
    except ValueError as error:
        assert "escapes session directory" in str(error)
