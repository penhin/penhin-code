from __future__ import annotations

from unittest.mock import patch

from penhin.auth.browser import open_browser


def test_linux_browser_launch_is_detached_shell_free_and_quiet() -> None:
    with (
        patch("penhin.auth.browser.sys.platform", "linux"),
        patch.dict("penhin.auth.secrets.os.environ", {"OPENAI_API_KEY": "must-not-propagate"}),
        patch("penhin.auth.browser.subprocess.Popen") as popen,
    ):
        open_browser("https://provider.example/authorize?state=value&scope=openid")

    command = popen.call_args.args[0]
    options = popen.call_args.kwargs
    assert command == ["xdg-open", "https://provider.example/authorize?state=value&scope=openid"]
    assert options["shell"] is False
    assert options["start_new_session"] is True
    assert options["stdin"] == options["stdout"] == options["stderr"]
    assert "OPENAI_API_KEY" not in options["env"]


def test_browser_launcher_failure_is_non_fatal() -> None:
    with patch("penhin.auth.browser.subprocess.Popen", side_effect=OSError("launcher missing")):
        open_browser("https://provider.example/authorize")
