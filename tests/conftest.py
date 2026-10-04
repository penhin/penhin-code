from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def protect_source_repository_from_agent_worktrees(monkeypatch: pytest.MonkeyPatch):
    """Tests must provision Agent and integration worktrees in temporary repos."""
    from penhin.orchestration import worktrees

    source_root = Path(__file__).resolve().parents[1]
    repository_root = worktrees.repository_root

    def checked_repository_root() -> Path:
        root = repository_root()
        if root.is_relative_to(source_root):
            pytest.fail("Agent worktree tests must use a temporary Git repository")
        return root

    monkeypatch.setattr(worktrees, "repository_root", checked_repository_root)


@pytest.fixture(autouse=True)
def isolated_orchestration_database(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Keep ordinary tests independent from developer .env database settings."""
    monkeypatch.setenv(
        "PENHIN_DATABASE_URL",
        f"sqlite:///{tmp_path / 'orchestration.sqlite3'}",
    )
    yield
