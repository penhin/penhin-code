import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands


def test_handle_local_command_ignores_normal_input() -> None:
    assert commands.handle_local_command("hello") is False


def test_handle_local_command_shows_help() -> None:
    with patch("commands.ui.print_info") as mocked_print_info:
        assert commands.handle_local_command("/help") is True

    mocked_print_info.assert_any_call("/workspace Show workspace summary")
    mocked_print_info.assert_any_call("/help Show local commands")


def test_handle_local_command_shows_workspace() -> None:
    workspace = {"cwd": "/tmp/project"}

    with patch("commands.workspace_info", return_value=workspace), patch("commands.ui.print_json") as mocked_print_json:
        assert commands.handle_local_command("/workspace") is True

    mocked_print_json.assert_called_once_with(workspace)


def test_handle_local_command_reports_unknown_command() -> None:
    with patch("commands.ui.print_error") as mocked_print_error:
        assert commands.handle_local_command("/missing") is True

    mocked_print_error.assert_called_once_with("Unknown command: /missing")


def test_complete_local_command_matches_prefix() -> None:
    assert commands.complete_local_command("/w", 0) == "/workspace"
    assert commands.complete_local_command("/wo", 0) == "/workspace"
    assert commands.complete_local_command("/h", 0) == "/help"
    assert commands.complete_local_command("/x", 0) is None
    assert commands.complete_local_command("/workspace", 1) is None


def test_setup_command_completion_skips_without_readline() -> None:
    with patch.object(commands, "readline", None):
        commands.setup_command_completion()


def test_setup_command_completion_registers_completer() -> None:
    class FakeReadline:
        __doc__ = ""

        def __init__(self):
            self.completer = None
            self.delims = None
            self.binding = None

        def set_completer(self, completer):
            self.completer = completer

        def set_completer_delims(self, delims):
            self.delims = delims

        def parse_and_bind(self, binding):
            self.binding = binding

    fake_readline = FakeReadline()

    with patch.object(commands, "readline", fake_readline):
        commands.setup_command_completion()

    assert fake_readline.completer is commands.complete_local_command
    assert fake_readline.delims == " \t\n"
    assert fake_readline.binding == "tab: complete"


def run_all() -> None:
    test_handle_local_command_ignores_normal_input()
    test_handle_local_command_shows_help()
    test_handle_local_command_shows_workspace()
    test_handle_local_command_reports_unknown_command()
    test_complete_local_command_matches_prefix()
    test_setup_command_completion_skips_without_readline()
    test_setup_command_completion_registers_completer()


if __name__ == "__main__":
    run_all()
    print("ok")
