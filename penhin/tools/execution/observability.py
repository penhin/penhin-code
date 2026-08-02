from __future__ import annotations

import hashlib
import json
import logging
from typing import Protocol

from penhin.result import Result
from penhin.tools.types import ToolInput


SAFE_INPUT_FIELDS = {"path", "name", "id", "index", "action", "limit", "line_numbers"}
HASHED_INPUT_FIELDS = {"command", "content", "task", "description", "note", "old", "new", "items", "blocked_by"}
logger = logging.getLogger("penhin.tool_runtime")
logger.addHandler(logging.NullHandler())


class ObservableToolRun(Protocol):
    result: Result
    manual_compact: bool
    approval_required: bool


def short_hash(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _value_size(value: object) -> int:
    return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def input_summary(tool_input: ToolInput) -> str:
    if not tool_input:
        return "<none>"
    parts = []
    for key in sorted(tool_input):
        value = tool_input[key]
        if key in SAFE_INPUT_FIELDS:
            parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        elif key in HASHED_INPUT_FIELDS:
            parts.extend((f"{key}_sha={json.dumps(short_hash(value))}", f"{key}_chars={_value_size(value)}"))
        else:
            parts.append(f"{key}=<hidden:{type(value).__name__}>")
    return " ".join(parts)


def _result_summary(result: Result) -> str:
    return " ".join(f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}" for key, value in result.summary().items())


def log_tool_start(tool_id: str, tool_name: str, tool_input: ToolInput) -> None:
    logger.info(f"[tool] start call_id={tool_id} name={tool_name} input={input_summary(tool_input)}")


def log_tool_done(tool_id: str, tool_name: str, tool_run: ObservableToolRun, duration_ms: float) -> None:
    result = tool_run.result
    flags = f"manual_compact={json.dumps(tool_run.manual_compact)} approval_required={json.dumps(tool_run.approval_required)}"
    summary = _result_summary(result)
    if result.ok:
        logger.info(f"[tool] done call_id={tool_id} name={tool_name} status=ok duration_ms={duration_ms:.2f} {flags} {summary}")
    else:
        logger.error(f"[tool] done call_id={tool_id} name={tool_name} status=error duration_ms={duration_ms:.2f} code={result.meta.get('code', 'unknown')} {flags} {summary}")


def log_tool_blocked(tool_id: str, tool_name: str, tool_input: ToolInput, result: Result, duration_ms: float, status: str) -> None:
    logger.warning(
        f"[tool] blocked call_id={tool_id} name={tool_name} status={status} duration_ms={duration_ms:.2f} "
        f"input={input_summary(tool_input)} code={result.meta.get('code', 'unknown')} {_result_summary(result)}"
    )


__all__ = ["input_summary", "log_tool_blocked", "log_tool_done", "log_tool_start", "short_hash"]
