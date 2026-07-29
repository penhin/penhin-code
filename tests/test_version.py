from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from main import parse_args
from config import get_version


def test_version_prefers_environment_override() -> None:
    with patch.dict("config.os.environ", {"PENHIN_VERSION": "1.2.3"}, clear=True):
        assert get_version() == "1.2.3"


def test_version_uses_installed_package_metadata() -> None:
    with (
        patch.dict("config.os.environ", {}, clear=True),
        patch("config.version", return_value="0.1.0"),
    ):
        assert get_version() == "0.1.0"


def test_version_falls_back_for_source_checkout() -> None:
    with (
        patch.dict("config.os.environ", {}, clear=True),
        patch("config.version", side_effect=PackageNotFoundError),
    ):
        assert get_version() == "dev"


def test_cli_exposes_version(capsys) -> None:
    with (
        patch.dict("config.os.environ", {"PENHIN_VERSION": "0.1.0-test"}, clear=True),
        pytest.raises(SystemExit) as exit_info,
    ):
        parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip().endswith(" 0.1.0-test")
