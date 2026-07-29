import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atomic_io


def test_atomic_write_cleans_temp_file_on_replace_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "target.txt"
        temp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            try:
                atomic_io.atomic_write_text(path, "content")
            except OSError:
                pass
            else:
                raise AssertionError("expected replace failure")

        assert not temp_path.exists()
        assert not path.exists()


def test_atomic_json_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "data.json"
        data = {"name": "penhin", "items": [1, 2]}

        atomic_io.write_json_atomic(path, data)

        assert atomic_io.read_json(path) == data


def test_atomic_jsonl_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "data.jsonl"
        items = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

        atomic_io.write_jsonl_atomic(path, items)

        assert atomic_io.read_jsonl(path) == items
