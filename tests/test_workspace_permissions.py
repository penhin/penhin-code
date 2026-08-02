import os
from unittest.mock import patch

from penhin.orchestration.permissions import readonly_command_is_allowed, write_is_allowed


def test_readonly_workspace_denies_write_capability() -> None:
    with patch.dict(os.environ, {"PENHIN_WORKSPACE_MODE": "readonly"}):
        assert write_is_allowed() is False
        assert readonly_command_is_allowed("git status --short") is True
        assert readonly_command_is_allowed("pytest -q") is True
        assert readonly_command_is_allowed("git status && touch unsafe") is False


def test_isolated_write_workspace_allows_writes() -> None:
    with patch.dict(os.environ, {"PENHIN_WORKSPACE_MODE": "isolated_write"}):
        assert write_is_allowed() is True
