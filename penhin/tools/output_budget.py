from __future__ import annotations

from dataclasses import dataclass


MAX_TOOL_OUTPUT_BYTES = 50_000
MAX_TOOL_OUTPUT_LINES = 2_000


@dataclass(frozen=True)
class BoundedOutput:
    text: str
    truncated: bool
    original_bytes: int
    original_lines: int


def bound_text(
    text: str,
    *,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
    max_lines: int = MAX_TOOL_OUTPUT_LINES,
    keep: str = "head",
) -> BoundedOutput:
    """Bound tool output by UTF-8 bytes and lines without splitting normal lines."""
    lines = text.splitlines()
    original_bytes = len(text.encode("utf-8"))
    original_lines = len(lines)
    if original_bytes <= max_bytes and original_lines <= max_lines:
        return BoundedOutput(text, False, original_bytes, original_lines)
    selected = lines[:max_lines] if keep == "head" else lines[-max_lines:]
    candidate = "\n".join(selected)
    encoded = candidate.encode("utf-8")

    if len(encoded) > max_bytes:
        fragment = encoded[:max_bytes] if keep == "head" else encoded[-max_bytes:]
        candidate = fragment.decode("utf-8", errors="ignore")
        if "\n" in candidate:
            candidate = candidate.rsplit("\n", 1)[0] if keep == "head" else candidate.split("\n", 1)[1]

    truncated = original_bytes > len(candidate.encode("utf-8")) or original_lines > len(selected)
    return BoundedOutput(candidate, truncated, original_bytes, original_lines)
