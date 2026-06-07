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


def run_all() -> None:
    test_handle_local_command_ignores_normal_input()
    test_handle_local_command_shows_help()
    test_handle_local_command_shows_workspace()
    test_handle_local_command_reports_unknown_command()


if __name__ == "__main__":
    run_all()
    print("ok")
