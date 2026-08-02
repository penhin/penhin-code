from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode if mode is not None else 0o666)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
        if mode is not None:
            os.chmod(path, mode)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_jsonl(path: Path) -> list[Any]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl_atomic(path: Path, items: list[Any]) -> None:
    lines = [
        json.dumps(item, ensure_ascii=False)
        for item in items
    ]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
