"""Repository implementations and backend selection for orchestration storage."""

from __future__ import annotations

import os
from pathlib import Path

from .protocol import OrchestrationRepository
from .sqlite_repository import SqliteOrchestrationRepository, sqlite_database_url


def database_url_from_env() -> str:
    configured = os.getenv("PENHIN_DATABASE_URL")
    if configured:
        return configured
    return sqlite_database_url(Path.cwd() / ".penhin" / "orchestration.sqlite3")


def repository_from_database_url(database_url: str) -> OrchestrationRepository:
    if database_url.startswith(("postgresql://", "postgres://")):
        from .postgres_repository import PostgresOrchestrationRepository
        return PostgresOrchestrationRepository(database_url)
    if database_url.startswith("sqlite:///"):
        return SqliteOrchestrationRepository(database_url)
    raise ValueError("PENHIN_DATABASE_URL must use postgresql://, postgres://, or sqlite:///")


__all__ = [
    "OrchestrationRepository",
    "SqliteOrchestrationRepository",
    "database_url_from_env",
    "repository_from_database_url",
]
