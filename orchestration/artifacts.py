from __future__ import annotations

from typing import Any


def normalize_subagent_result(text: str, *, changed_files: list[str] | None = None) -> tuple[dict[str, Any], bool]:
    """Preserve raw model output while exposing a stable handoff contract."""
    content = {
        "summary": text.strip(),
        "findings": [],
        "commands_run": [],
        "changed_files": changed_files or [],
        "risks": [],
        "handoff": "Review the summary and decide whether to schedule a dependent job.",
        "raw_text": text,
    }
    return content, bool(text.strip())
