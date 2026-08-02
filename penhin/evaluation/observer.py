from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from penhin.auth.secrets import safe_value


_observer: ContextVar["EvaluationObserver | None"] = ContextVar("penhin_evaluation_observer", default=None)
_write_lock = threading.Lock()
CORRELATION_ENV = {
    "trace_id": "PENHIN_TRACE_ID",
    "root_task_id": "PENHIN_ROOT_TASK_ID",
    "job_id": "PENHIN_JOB_ID",
    "attempt_id": "PENHIN_ATTEMPT_ID",
}


def anonymous_id(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class EvaluationObserver:
    def __init__(self, run_dir: Path, run_id: str, case_id: str = "", repetition: int = 0):
        self.run_dir = run_dir.resolve()
        self.run_id = run_id
        self.case_id = case_id
        self.repetition = repetition

    @property
    def event_path(self) -> Path:
        return self.run_dir / "events" / f"{os.getpid()}.jsonl"

    def emit(self, event_type: str, **payload: Any) -> None:
        correlation = {
            name: value
            for name, environment_name in CORRELATION_ENV.items()
            if (value := os.getenv(environment_name, ""))
        }
        event = {
            "schema_version": "penhin.eval.event/v2",
            "event_id": str(uuid4()),
            "event_type": event_type,
            "timestamp_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            "run_id": self.run_id,
            "case_id": self.case_id,
            "repetition": self.repetition,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "correlation": correlation,
            "payload": payload,
        }
        event = safe_value(event, max_string_chars=4000)
        path = self.event_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def current_observer() -> EvaluationObserver | None:
    observer = _observer.get()
    if observer is not None:
        return observer
    run_dir = os.getenv("PENHIN_EVAL_RUN_DIR")
    run_id = os.getenv("PENHIN_EVAL_RUN_ID")
    if not run_dir or not run_id:
        return None
    observer = EvaluationObserver(
        Path(run_dir), run_id, os.getenv("PENHIN_EVAL_CASE_ID", ""), int(os.getenv("PENHIN_EVAL_REPETITION", "0") or 0),
    )
    _observer.set(observer)
    return observer


def emit(event_type: str, **payload: Any) -> None:
    observer = current_observer()
    if observer is not None:
        observer.emit(event_type, **payload)


@contextmanager
def observing(observer: EvaluationObserver) -> Iterator[EvaluationObserver]:
    token = _observer.set(observer)
    try:
        yield observer
    finally:
        _observer.reset(token)


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((run_dir / "events").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return sorted(events, key=lambda item: (item.get("timestamp_ns", 0), item.get("pid", 0)))
