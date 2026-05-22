from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
