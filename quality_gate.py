from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


def _run(name: str, command: list[str], workdir: Path) -> GateCheck:
    result = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=900, check=False)
    detail = (result.stdout + result.stderr).strip()
    if len(detail) > 2000:
        detail = detail[-2000:]
    return GateCheck(name, result.returncode == 0, detail or "passed")


def run_quality_gate(workdir: Path | None = None) -> list[GateCheck]:
    """Run deterministic repository checks used before integration promotion."""
    root = (workdir or Path.cwd()).resolve()
    return [
        _run("syntax", [sys.executable, "-m", "compileall", "-q", "."], root),
        _run("diff-whitespace", ["git", "diff", "--check"], root),
        _run("tests", [sys.executable, "-m", "pytest", "-q"], root),
    ]
