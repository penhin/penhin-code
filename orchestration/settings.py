from __future__ import annotations

import logging
import os


logger = logging.getLogger("penhin.orchestration")


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default


def sync_agent_timeout_seconds() -> int:
    return env_int("PENHIN_SYNC_AGENT_TIMEOUT_SECONDS", 900, 1)


def agent_poll_interval_seconds() -> float:
    return env_float("PENHIN_AGENT_POLL_INTERVAL_SECONDS", 0.1, 0.01)


def scheduler_workers() -> int:
    return env_int("PENHIN_SCHEDULER_WORKERS", 2, 1)


def worker_kill_grace_seconds() -> float:
    return env_float("PENHIN_WORKER_KILL_GRACE_SECONDS", 2.0, 0.1)


def sqlite_connect_timeout_seconds() -> float:
    return env_float("PENHIN_SQLITE_CONNECT_TIMEOUT_SECONDS", 5.0, 0.1)


def sqlite_busy_timeout_ms() -> int:
    return env_int("PENHIN_SQLITE_BUSY_TIMEOUT_MS", 5000, 1)
