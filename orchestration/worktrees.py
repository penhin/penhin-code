from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentWorktree:
    path: str
    branch: str


def repository_root() -> Path:
    from auth.secrets import scrubbed_environment
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5, check=False, env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError("Agent worktrees require a Git repository")
    return Path(result.stdout.strip()).resolve()


def provision_worktree(job_id: str) -> AgentWorktree:
    from auth.secrets import scrubbed_environment
    root = repository_root()
    worktree_path = root / ".penhin" / "worktrees" / job_id
    branch = f"penhin/agent-{job_id.replace('-', '')[:12]}"
    if worktree_path.exists():
        raise RuntimeError(f"Refusing to reuse existing agent worktree: {worktree_path}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree add failed")
    return AgentWorktree(path=str(worktree_path), branch=branch)


def provision_integration_worktree(run_id: str, base_commit: str) -> AgentWorktree:
    from auth.secrets import scrubbed_environment
    root = repository_root()
    worktree_path = root / ".penhin" / "integrations" / run_id
    branch = f"penhin/integration-{run_id.replace('-', '')[:12]}"
    if worktree_path.exists():
        raise RuntimeError(f"Refusing to reuse existing integration worktree: {worktree_path}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base_commit],
        cwd=root, capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree add failed")
    return AgentWorktree(path=str(worktree_path), branch=branch)
