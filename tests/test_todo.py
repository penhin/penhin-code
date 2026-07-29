import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from todo import TODO_FILE, run_todo

from tests.helpers import run_spec_tool


def test_todo_flow() -> None:
    original = TODO_FILE.read_text(encoding="utf-8") if TODO_FILE.exists() else None
    try:
        set_result = run_todo("set", ["inspect", "verify"])
        assert set_result.ok is True
        assert "1. [ ] inspect" in set_result.message
        assert "2. [ ] verify" in set_result.message

        done_result = run_todo("done", index=1)
        assert done_result.ok is True
        assert "1. [x] inspect" in done_result.message

        show_result = run_todo("show")
        assert show_result.ok is True
        assert "1. [x] inspect" in show_result.message

        clear_result = run_todo("clear")
        assert clear_result.ok is True
        assert clear_result.message == "Cleared todos"
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

        assert set_result.ok is True
        assert show_result.ok is True
        assert "1. [ ] inspect" in show_result.message
        assert done_result.ok is True
        assert "1. [x] inspect" in done_result.message
        assert clear_result.ok is True
        assert clear_result.message == "Cleared todos"
    finally:
        if original is None:
            TODO_FILE.unlink(missing_ok=True)
        else:
            TODO_FILE.write_text(original, encoding="utf-8")


def run_all() -> None:
    test_todo_flow()
    test_todo_tool_handlers()


if __name__ == "__main__":
    run_all()
    print("ok")
