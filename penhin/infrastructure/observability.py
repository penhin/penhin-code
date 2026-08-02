from __future__ import annotations

import os
import resource
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    rss_bytes: int

    @property
    def rss_label(self) -> str:
        value = float(self.rss_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GiB"


def process_snapshot() -> ProcessSnapshot:
    """Return the current process PID and resident memory without optional dependencies."""
    rss_bytes = 0
    statm = Path("/proc/self/statm")
    try:
        rss_pages = int(statm.read_text(encoding="utf-8").split()[1])
        rss_bytes = rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (FileNotFoundError, IndexError, OSError, ValueError):
        # macOS reports bytes while Linux reports KiB for ru_maxrss.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_bytes = peak if os.uname().sysname == "Darwin" else peak * 1024
    return ProcessSnapshot(pid=os.getpid(), rss_bytes=rss_bytes)


def cli_status_line() -> str:
    snapshot = process_snapshot()
    return f"PID {snapshot.pid}  ·  Memory {snapshot.rss_label}"
