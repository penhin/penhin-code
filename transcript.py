import time
from typing import Any

from pathlib import Path

from atomic_io import read_jsonl, write_jsonl_atomic


TRANSCRIPT_DIR = Path(".transcripts")


def serialize_for_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [serialize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_for_json(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return serialize_for_json(value.model_dump(mode="json"))
    return {
        "type": value.__class__.__name__,
        "value": str(value),
    }


class TranscriptStore:
    def __init__(self, transcript_dir: Path):
        self.transcript_dir = transcript_dir
    
    def save(self, messages: list[Any]) -> Path:
        self.transcript_dir.mkdir(exist_ok=True)
        transcript_path = self.transcript_dir / f"transcript_{time.time_ns()}.jsonl"
        write_jsonl_atomic(
            transcript_path,
            [serialize_for_json(msg) for msg in messages],
        )
        return transcript_path
    
    def latest(self) -> Path | None:
        if not self.transcript_dir.exists():
            return None

        paths = sorted(self.transcript_dir.glob("transcript_*.jsonl"))
        return paths[-1] if paths else None

    def read(self, path: Path) -> list[dict[str, Any]]:
        resolved = path.resolve()
        base = self.transcript_dir.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Transcript path escapes transcript directory: {path}")
        if resolved.suffix != ".jsonl":
            raise ValueError(f"Transcript path must be a .jsonl file: {path}")

        return read_jsonl(resolved)


transcripts = TranscriptStore(TRANSCRIPT_DIR)
