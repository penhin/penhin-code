from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from .models import CheckResult, EvaluationCase


EVALUATION_INFRASTRUCTURE_PATHS = (
    ".penhin", ".penhin/**",
    ".tasks", ".tasks/**",
    ".penhin_todos.json",
    ".pytest_cache", ".pytest_cache/**",
    "__pycache__", "**/__pycache__/**",
    "*.pyc", "**/*.pyc",
)


def git_changed_files(workdir: Path, base_commit: str = "") -> list[str]:
    from auth.secrets import scrubbed_environment
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workdir,
        capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    paths = {line[3:].split(" -> ")[-1] for line in result.stdout.splitlines() if len(line) >= 4}
    if base_commit:
        committed = subprocess.run(
            ["git", "diff", "--name-only", f"{base_commit}..HEAD", "--", "."], cwd=workdir,
            capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment(),
        )
        if committed.returncode:
            raise RuntimeError(committed.stderr.strip() or "git diff --name-only failed")
        paths.update(path for path in committed.stdout.splitlines() if path)
    # The isolated runner deliberately places its database and orchestration
    # worktrees under .penhin. They are harness state, not candidate changes.
    return sorted(path for path in paths if not _matches(path, EVALUATION_INFRASTRUCTURE_PATHS))


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def grade_case(case: EvaluationCase, workdir: Path, base_commit: str = "") -> tuple[list[CheckResult], list[str], list[str]]:
    changed = git_changed_files(workdir, base_commit)
    checks: list[CheckResult] = []
    violations: list[str] = []
    if case.allowed_paths:
        outside = [path for path in changed if not _matches(path, case.allowed_paths)]
        checks.append(CheckResult("allowed_paths", not outside, "outside allowed paths: " + ", ".join(outside) if outside else "all changes allowed"))
        violations.extend(f"out-of-scope change: {path}" for path in outside)
    forbidden = [path for path in changed if _matches(path, case.forbidden_paths)]
    checks.append(CheckResult("forbidden_paths", not forbidden, "forbidden changes: " + ", ".join(forbidden) if forbidden else "no forbidden changes"))
    violations.extend(f"forbidden change: {path}" for path in forbidden)
    for item in case.content_checks:
        path = workdir / item.path
        resolved = path.resolve()
        if not resolved.is_relative_to(workdir.resolve()):
            checks.append(CheckResult(f"content:{item.path}", False, f"path escapes evaluation workspace: {item.path}"))
            violations.append(f"content check escaped workspace: {item.path}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
            passed = item.contains in content
            detail = f"{item.contains!r} {'found' if passed else 'not found'} in {item.path}"
        except OSError as error:
            passed, detail = False, f"unable to read {item.path}: {error}"
        checks.append(CheckResult(f"content:{item.path}", passed, detail))
    for index, item in enumerate(case.commands):
        try:
            from auth.secrets import redact_text, scrubbed_environment
            result = subprocess.run(
                item.command, cwd=workdir, capture_output=True, text=True,
                timeout=item.timeout_seconds, check=False, env=scrubbed_environment(),
            )
            output = redact_text((result.stdout + result.stderr).strip())[-2000:]
            checks.append(CheckResult(f"command:{index}", result.returncode == 0, output or f"exit={result.returncode}"))
        except (OSError, subprocess.TimeoutExpired) as error:
            checks.append(CheckResult(f"command:{index}", False, str(error)))
    return checks, changed, violations


def diff_summary(workdir: Path, limit: int = 8000, base_commit: str = "") -> str:
    command = ["git", "diff", "--stat"]
    if base_commit:
        command.append(f"{base_commit}..HEAD")
    command.extend(["--", "."])
    from auth.secrets import scrubbed_environment
    result = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment())
    return result.stdout.strip()[:limit]
