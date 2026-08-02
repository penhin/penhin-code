from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def atomic_write_text(
    path: Path,
    content: str,
    mode: int | None = None,
    *,
    fsync_directory: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        if fsync_directory:
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(
    path: Path,
    data: Any,
    *,
    sort_keys: bool = False,
    trailing_newline: bool = False,
    mode: int | None = None,
    fsync_directory: bool = False,
) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys)
    atomic_write_text(
        path,
        content + ("\n" if trailing_newline else ""),
        mode,
        fsync_directory=fsync_directory,
    )


def write_safe_json_atomic(path: Path, data: Any) -> None:
    from penhin.auth.secrets import safe_value

    write_json_atomic(path, safe_value(data), sort_keys=True, trailing_newline=True)


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
