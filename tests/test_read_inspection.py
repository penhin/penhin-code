"""Workspace inspection through the model's public read invocation."""
import pytest

from penhin.tools.execution import run_tool, runtime_permission_setup
from penhin.tools.registry import MODEL_TOOL_CATALOG


@pytest.fixture
def inspect(tmp_path, monkeypatch):
    for module in ("files", "workspace", "glob"):
        monkeypatch.setattr(f"penhin.tools.builtin.{module}.WORKDIR", tmp_path)
    policy, approval = runtime_permission_setup("full-access")

    def invoke(**arguments):
        return run_tool("read", arguments, policy, approval, catalog=MODEL_TOOL_CATALOG).result

    return invoke


def test_directory_pages_are_sorted_and_discover_new_files(tmp_path, inspect):
    for name in ("c.py", "a.py", "b.txt"):
        (tmp_path / name).write_text(name)
    first = inspect(path=".", limit=2)
    assert first.ok
    assert first.data["paths"] == ["a.py", "b.txt"]
    assert first.data["next_offset"] == 3
    assert not first.data["complete"]
    second = inspect(path=".", limit=2, offset=first.data["next_offset"])
    assert second.data["paths"] == ["c.py"]
    assert second.data["complete"]
    assert second.data["next_offset"] is None
    matched = inspect(path=".", pattern="**/*.py", limit=1)
    assert matched.data["paths"] == ["a.py"]
    assert inspect(path=".", pattern="**/*.py", offset=matched.data["next_offset"]).data["paths"] == ["c.py"]
    (tmp_path / "d.py").write_text("new")
    assert inspect(path=".", pattern="**/*.py").data["paths"] == ["a.py", "c.py", "d.py"]


def test_search_is_literal_bounded_and_excludes_ignored_files(tmp_path, inspect):
    (tmp_path / "app.py").write_text("first needle.*\nsecond needle.*\nnot a match\n")
    (tmp_path / ".env").write_text("needle.* secret\n")
    result = inspect(path=".", query="needle.*", limit=1)
    assert result.ok
    assert result.message == "app.py:1:first needle.*"
    assert result.meta["truncated"]
    assert result.data["narrow_query_to_continue"]
    assert inspect(path="app.py", query="second").message == "app.py:2:second needle.*"
    assert inspect(path=".", query="secret").message == ""


@pytest.mark.parametrize("mode", [{}, {"pattern": "**/*"}, {"query": "secret"}])
@pytest.mark.parametrize("path", [".env", "../outside.py", "external.py"])
def test_inspection_cannot_escape_workspace_or_read_ignored_paths(tmp_path, inspect, path, mode):
    (tmp_path / ".env").write_text("secret")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret")
    (tmp_path / "external.py").symlink_to(outside)
    result = inspect(path=path, **mode)
    assert not result.ok
    assert "secret" not in result.message


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"pattern": ""}, {"query": "hello", "pattern": "*.py"},
    {"query": "hello", "offset": 2}, {"query": "hello", "snapshot_id": "old"},
    {"snapshot_id": "old"}, {"offset": 0}, {"limit": -1},
])
def test_invalid_inspection_arguments_fail_explicitly(inspect, arguments):
    assert not inspect(path=".", **arguments).ok


def test_file_reads_keep_snapshot_continuation(tmp_path, inspect):
    file = tmp_path / "app.py"
    file.write_text("first\nsecond\n")
    first = inspect(path="app.py", limit=1)
    file.write_text("changed\n")
    continuation = inspect(path="app.py", offset=first.data["next_offset"], snapshot_id=first.data["snapshot_id"])
    assert continuation.ok
    assert continuation.message == "2: second"
    assert continuation.data["complete"]
    assert inspect(path="app.py").message == "1: changed"


@pytest.mark.parametrize("pattern", [None, "**/*.py"])
def test_large_directory_continuation_never_returns_cache_placeholders(tmp_path, inspect, pattern):
    names = [f"module_{index:04}.py" for index in range(350)]
    for name in names:
        (tmp_path / name).write_text("")
    arguments = {"path": ".", "limit": 10}
    if pattern is not None:
        arguments["pattern"] = pattern
    first = inspect(**arguments)
    second = inspect(**arguments, offset=first.data["next_offset"])
    assert second.ok, second.error
    assert second.data["paths"] == names[10:20]
    assert inspect(**arguments).data["paths"] == names[:10]


def test_directory_byte_limit_keeps_complete_paths_and_continuation(tmp_path, inspect):
    from penhin.tools.output_budget import MAX_TOOL_OUTPUT_BYTES

    names = ["文" * 70 + f"_{index:03}.py" for index in range(250)]
    for name in names:
        (tmp_path / name).write_text("")
    first = inspect(path=".")
    assert first.ok
    assert len(first.message.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    assert first.meta["truncated"]
    assert first.message.splitlines() == first.data["paths"]
    second = inspect(path=".", offset=first.data["next_offset"])
    assert second.ok
    assert first.data["paths"] + second.data["paths"] == names
    assert second.data["complete"]
