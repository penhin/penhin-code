"""Read-only prerequisites inventory; never a claim of Sandbox enforcement."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


@dataclass(frozen=True)
class PrerequisiteCheck:
    code: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class SandboxPreflight:
    platform: str
    kernel: str
    python: str
    checks: tuple[PrerequisiteCheck, ...]

    @property
    def ready_for_composition(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    @property
    def production_ready(self) -> bool:
        # #60 preparation must not enable #61/#62 production activation.
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "ready_for_composition": self.ready_for_composition,
            "production_ready": self.production_ready,
            "tested_native_baseline": None,
            "scope": "prerequisites_only",
        }


def inspect_linux_sandbox() -> SandboxPreflight:
    """Inventory the candidate backend without starting Plugin code."""
    kernel = platform.release()
    check = PrerequisiteCheck(
        "unsupported_platform", False,
        "A native Linux host is required; Windows and unvalidated WSL profiles are unsupported.",
    )
    if sys.platform == "linux" and (
        "microsoft" in kernel.lower() or "wsl" in kernel.lower()
        or "WSL_INTEROP" in os.environ or "WSL_DISTRO_NAME" in os.environ
        or Path("/run/WSL").exists()
    ):
        check = PrerequisiteCheck(
            "unvalidated_wsl_profile", False,
            "WSL needs separate Windows-interop and mounted-host-path acceptance evidence.",
        )
    elif sys.platform == "linux" and os.geteuid() == 0:
        check = PrerequisiteCheck(
            "root_not_supported", False,
            "Run as an ordinary user with an existing systemd user manager; do not use sudo.",
        )
    elif sys.platform == "linux":
        return SandboxPreflight(
            sys.platform, kernel, platform.python_version(), tuple(_native_checks()),
        )
    return SandboxPreflight(sys.platform, kernel, platform.python_version(), (check,))


def _run(executable: str, *arguments: str) -> tuple[bool, str]:
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if key in os.environ:
            environment[key] = os.environ[key]
    try:
        result = subprocess.run(
            [executable, *arguments], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{type(error).__name__}: {error}"
    output = (result.stdout or result.stderr).strip()
    return result.returncode == 0, output[:1024]


def _native_checks() -> list[PrerequisiteCheck]:
    checks: list[PrerequisiteCheck] = []
    executables: dict[str, str] = {}
    # Only trusted system tool directories, never the caller's broad PATH.
    for name in ("bwrap", "systemctl", "unshare", "mount", "cc", "pkg-config", "readelf"):
        executable = next((
            path for directory in ("/usr/bin", "/bin")
            if (path := Path(directory) / name).is_file() and os.access(path, os.X_OK)
        ), None)
        if executable is None:
            checks.append(PrerequisiteCheck(f"missing_{name}", False, f"Required system tool {name} is missing."))
            continue
        executables[name] = str(executable)
        ok, detail = _run(str(executable), "--version")
        checks.append(PrerequisiteCheck(f"tool_{name}", ok and bool(detail), detail.splitlines()[0] if detail else "No version output."))

    try:
        controllers = set(Path("/sys/fs/cgroup/cgroup.controllers").read_text(encoding="ascii").split())
    except OSError:
        controllers = set()
    checks.append(PrerequisiteCheck(
        "cgroup_v2_unavailable" if not controllers else "cgroup_v2_controllers",
        {"cpu", "memory", "pids"} <= controllers,
        "Visible controllers: " + ", ".join(sorted(controllers))
        + ". Actual rootless delegation and placement must pass the composition probe.",
    ))

    if "systemctl" in executables:
        ok, version = _run(executables["systemctl"], "--user", "show", "--property=Version", "--value")
        match = re.match(r"v?(\d+)", version)
        checks.append(PrerequisiteCheck(
            "systemd_user_manager", ok and match is not None and int(match[1]) >= 257,
            f"User manager: {version or 'unreachable'}. The candidate FD handoff requires systemd 257 or newer.",
        ))
    if "pkg-config" in executables:
        for library in ("libsystemd", "libseccomp"):
            ok, version = _run(executables["pkg-config"], "--modversion", library)
            checks.append(PrerequisiteCheck(
                f"development_{library}", ok,
                f"{library}: {version or 'development files unavailable'}. Needed to compile the trusted probe.",
            ))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    report = inspect_linux_sandbox()
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.ready_for_composition else 1


if __name__ == "__main__":
    raise SystemExit(main())
