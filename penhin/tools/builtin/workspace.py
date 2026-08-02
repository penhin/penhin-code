import json
import os
import subprocess
from pathlib import Path

from penhin.result import Result


WORKDIR = Path.cwd().resolve()
IGNORED_PATH_PARTS = [
    ".git",
    ".penhin",
    ".penhin_todos.json",
    ".tasks",
    ".transcripts",
    ".venv",
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "__pycache__",
    "skills",
]


def is_ignored_path(path: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(WORKDIR).parts
    except ValueError:
        return True

    return any(part in IGNORED_PATH_PARTS or part.startswith(".env.") for part in relative_parts)


def iter_workspace_files(root: Path):
    if root.is_file():
        yield root
        return

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            dirname for dirname in dirnames
            if not is_ignored_path(current / dirname)
        ]
        for filename in filenames:
            child = current / filename
            if not is_ignored_path(child):
                yield child


def run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        from penhin.auth.secrets import scrubbed_environment
        result = subprocess.run(
            ["git", *args],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
            env=scrubbed_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result


def git_branch_name() -> str:
    result = run_git(["branch", "--show-current"])
    if result is None:
        return "-"
    return result.stdout.strip() or "-"


def git_dirty_files_count() -> int | None:
    result = run_git(["status", "--short"])
    if result is None:
        return None
    return len([
        line for line in result.stdout.splitlines()
        if line.strip()
    ])


def test_command_hint() -> str:
    if (WORKDIR / "pytest.ini").exists() or (WORKDIR / "pyproject.toml").exists():
        return ".venv/bin/python -m pytest -q"
    return "-"


def workspace_info(tool_names: list[str] | None = None) -> dict[str, object]:
    return {
        "cwd": str(WORKDIR),
        "git_branch": git_branch_name(),
        "dirty_files_count": git_dirty_files_count(),
        "has_agents_md": (WORKDIR / "AGENTS.md").exists(),
        "ignored": IGNORED_PATH_PARTS,
        "test_command_hint": test_command_hint(),
        "tools": tool_names or [],
    }


def run_workspace(tool_names: list[str] | None = None) -> Result:
    info = workspace_info(tool_names)
    return Result.success(json.dumps(info, ensure_ascii=False, indent=2), data=info)
