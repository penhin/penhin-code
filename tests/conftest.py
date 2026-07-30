from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_orchestration_database(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Keep ordinary tests independent from developer .env database settings."""
    monkeypatch.setenv(
        "PENHIN_DATABASE_URL",
        f"sqlite:///{tmp_path / 'orchestration.sqlite3'}",
    )
    yield
