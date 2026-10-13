from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from penhin.sandbox.preflight import inspect_linux_sandbox, main


def test_windows_reports_unavailable_without_attempting_linux_commands(monkeypatch, capsys):
    monkeypatch.setattr("sys.platform", "win32")
    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    assert report.checks[0].code == "unsupported_platform"
    assert not report.production_ready
    assert main([]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ready_for_composition"] is False
    assert output["production_ready"] is False
    assert output["checks"][0]["code"] == "unsupported_platform"


def test_wsl_requires_its_own_validated_profile(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.6.87.2-microsoft-standard-WSL2")

    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    assert report.checks[0].code == "unvalidated_wsl_profile"


def test_inherited_wsl_channels_are_not_treated_as_a_native_profile(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.12.0-custom")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/123_interop")

    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    assert report.checks[0].code == "unvalidated_wsl_profile"


def test_linux_root_does_not_satisfy_rootless_prerequisite(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.12.0")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)

    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    assert report.checks[0].code == "root_not_supported"


def test_native_linux_reports_missing_tools_and_cgroups_as_failures(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.12.0")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "")

    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    failures = {check.code for check in report.checks if not check.ok}
    assert {"missing_bwrap", "missing_systemctl", "cgroup_v2_unavailable"} <= failures
    assert report.to_dict()["tested_native_baseline"] is None


@pytest.mark.parametrize("manager_version,ready", [("256", False), ("257", True), ("", False)])
def test_user_manager_fd_support_is_required_but_never_proves_enforcement(monkeypatch, manager_version, ready):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.12.0")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("os.access", lambda *args: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "cpu memory pids")

    def system_tool(arguments, **kwargs):
        output = manager_version if "--user" in arguments else "installed tool version"
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", system_tool)

    report = inspect_linux_sandbox()

    assert report.ready_for_composition is ready
    assert report.production_ready is False
    assert report.to_dict()["tested_native_baseline"] is None


def test_timed_out_tools_are_reported_without_throwing(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("platform.release", lambda: "6.12.0")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("os.access", lambda *args: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "cpu memory pids")

    def timeout(arguments, **kwargs):
        raise subprocess.TimeoutExpired(arguments, 5)

    monkeypatch.setattr(subprocess, "run", timeout)

    report = inspect_linux_sandbox()

    assert not report.ready_for_composition
    assert any("TimeoutExpired" in check.detail for check in report.checks)
