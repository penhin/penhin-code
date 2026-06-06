from __future__ import annotations

import time
import re
import json
import logging
from typing import Any

from pathlib import Path
from dataclasses import dataclass

from atomic_io import read_jsonl, write_jsonl_atomic


logger = logging.getLogger("penhin.transcript")


TRANSCRIPT_DIR = Path(".transcripts")
COMPACT_TRANSCRIPT_RE = re.compile(r"^\[Conversation compressed\. Transcript: (?P<path>[^\]]+)\]")


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


@dataclass(frozen=True)
class SessionSummary:
    id: str
    path: Path
    updated_at: float
    message_count: int
    first_user: str


@dataclass(frozen=True)
class SessionInspect:
    id: str
    path: Path
    message_count: int
    role_counts: dict[str, int]
    first_user: str
    last_user: str
    last_assistant: str
    tool_result_count: int
    failed_tool_result_count: int
    event_count: int
    recent_events: list[str]


def summarize_content(content: Any, limit: int = 80) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif isinstance(item, str):
                text_parts.append(item)
        text = " ".join(text_parts)
    else:
        text = str(content) if content is not None else ""

    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def is_synthetic_user_message(message: dict[str, Any]) -> bool:
    content = message.get("content")

    if isinstance(content, str):
        return content.startswith("[Conversation compressed.")

    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )

    return False


def first_message_summary(messages: list[dict[str, Any]], role: str) -> str:
    for message in messages:
        if role == "user" and is_synthetic_user_message(message):
            continue
        if message.get("role") == role:
            return summarize_content(message.get("content"))    
    return ""


def last_message_summary(messages: list[dict[str, Any]], role: str) -> str:
    for message in reversed(messages):
        if role == "user" and is_synthetic_user_message(message):
            continue
        if message.get("role") == role:
            return summarize_content(message.get("content"))
    return ""


def iter_tool_results(messages: list[dict[str, Any]]):
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                yield item


def tool_result_failed(tool_result: dict[str, Any]) -> bool:
    if tool_result.get("is_error") is True:
        return True

    content = tool_result.get("content")
    if not isinstance(content, str):
        return False

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return False

    if result.get("ok") is False:
        return True
    return result.get("exit_code", 0) != 0


def tool_result_error_code(tool_result: dict[str, Any]) -> str:
    content = tool_result.get("content")
    if not isinstance(content, str):
        return "-"

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return "-"

    meta = result.get("meta")
    if not isinstance(meta, dict):
        return "-"

    code = meta.get("code")
    if not isinstance(code, str) or not code:
        return "-"
    return code


def session_event_timeline(messages: list[dict[str, Any]], limit: int | None = 8) -> list[str]:
    events = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "user" and is_synthetic_user_message(message):
            for tool_result in iter_tool_results([message]):
                status = "error" if tool_result_failed(tool_result) else "ok"
                tool_use_id = tool_result.get("tool_use_id", "-")
                tool_name = tool_result.get("tool_name", "-")
                event = f"tool_result | {status} | {tool_name} | {tool_use_id}"
                if status == "error":
                    event = f"{event} | {tool_result_error_code(tool_result)}"
                events.append(event)
            continue

        if role in {"user", "assistant"}:
            summary = summarize_content(content)
            if summary:
                events.append(f"{role} | {summary}")

    if limit is None:
        return events
    if limit <= 0:
        return []
    return events[-limit:]


def session_id_from_path(path: Path) -> str:
    if path.name.startswith("transcript_"):
        return path.stem.removeprefix("transcript_")
    return path.stem


def normalize_session_ref(session_ref: str) -> str:
    name = Path(session_ref).name
    if name.endswith(".jsonl"):
        name = Path(name).stem
    if name.startswith("transcript_"):
        name = name.removeprefix("transcript_")
    return name


def compact_transcript_ref(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if not isinstance(content, str):
        return None

    match = COMPACT_TRANSCRIPT_RE.match(content)
    if match is None:
        return None
    return match.group("path")


def serialized_message(message: Any) -> str:
    return json_dumps_for_compare(serialize_for_json(message))


def json_dumps_for_compare(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def merge_message_history(base: list[Any], tail: list[Any]) -> list[Any]:
    base_keys = [serialized_message(message) for message in base]
    tail_keys = [serialized_message(message) for message in tail]
    max_overlap = min(len(base_keys), len(tail_keys))

    for overlap in range(max_overlap, 0, -1):
        if base_keys[-overlap:] == tail_keys[:overlap]:
            return base + tail[overlap:]

    return base + tail


class TranscriptStore:
    def __init__(self, transcript_dir: Path):
        self.transcript_dir = transcript_dir
    
    def save(self, messages: list[Any]) -> Path:
        self.transcript_dir.mkdir(exist_ok=True)
        transcript_path = self.transcript_dir / f"transcript_{time.time_ns()}.jsonl"
        messages = self.expand_compacted_history(messages)
        write_jsonl_atomic(
            transcript_path,
            [serialize_for_json(msg) for msg in messages],
        )
        return transcript_path
    
    def save_to(self, path: Path, messages: list[Any]) -> Path:
        self.transcript_dir.mkdir(exist_ok=True)
        messages = self.expand_compacted_history(messages)
        write_jsonl_atomic(
            path,
            [serialize_for_json(msg) for msg in messages],
        )
        return path

    def expand_compacted_history(self, messages: list[Any]) -> list[Any]:
        if not messages:
            return messages
        if not isinstance(messages[0], dict):
            return messages

        previous_ref = compact_transcript_ref(messages[0])
        if previous_ref is None:
            return messages

        try:
            previous_messages = self.read(Path(previous_ref))
        except Exception:
            return messages

        return merge_message_history(previous_messages, messages[1:])
    
    def resolve_session_ref(self, session_ref: str) -> Path:
        raw_path = Path(session_ref)
        candidates = [
            raw_path,
            self.transcript_dir / raw_path,
            self.transcript_dir / f"transcript_{session_ref}.jsonl",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        prefix_match = self.resolve_session_prefix(session_ref)
        if prefix_match is not None:
            return prefix_match

        raise FileNotFoundError(f"Session not found: {session_ref}")

    def resolve_session_prefix(self, session_ref: str) -> Path | None:
        if not self.transcript_dir.exists():
            return None

        normalized = normalize_session_ref(session_ref)
        if not normalized:
            return None

        matches = [
            path
            for path in self.transcript_dir.glob("transcript_*.jsonl")
            if session_id_from_path(path).startswith(normalized)
        ]
        if not matches:
            return None

        matches.sort(key=lambda path: (len(session_id_from_path(path)), path.stat().st_mtime), reverse=True)
        return matches[0]
    
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

    def list(self) -> list[SessionSummary]:
        if not self.transcript_dir.exists():
            return []

        summaries = []
        for path in sorted(self.transcript_dir.glob("transcript_*.jsonl")):
            try:
                messages = self.read(path)
            except Exception:
                continue

            summaries.append(
                SessionSummary(
                    id=session_id_from_path(path),
                    path=path,
                    updated_at=path.stat().st_mtime,
                    message_count=len(messages),
                    first_user=first_message_summary(messages, "user"),
                )
            )
        return summaries

    def inspect(self, session_ref: str, event_limit: int = 8) -> SessionInspect:
        path = self.resolve_session_ref(session_ref)
        messages = self.read(path)
        role_counts: dict[str, int] = {}
        for message in messages:
            role = str(message.get("role", "unknown"))
            role_counts[role] = role_counts.get(role, 0) + 1
        tool_results = list(iter_tool_results(messages))
        events = session_event_timeline(messages, limit=None)

        return SessionInspect(
            id=session_id_from_path(path),
            path=path,
            message_count=len(messages),
            role_counts=role_counts,
            first_user=first_message_summary(messages, "user"),
            last_user=last_message_summary(messages, "user"),
            last_assistant=last_message_summary(messages, "assistant"),
            tool_result_count=len(tool_results),
            failed_tool_result_count=sum(
                1 for tool_result in tool_results
                if tool_result_failed(tool_result)
            ),
            event_count=len(events),
            recent_events=session_event_timeline(messages, limit=event_limit),
        )
    
    def load_session(
        self,
        resume: bool,
        session_ref: Path | str | None = None,
    ) -> tuple[list[dict], Path | None]:
        if not resume:
            logger.info("[session] new reason=flag")
            return [], None

        try:
            if session_ref is None:
                history_file = self.latest()
                if history_file is None:
                    logger.info("[session] new reason=no_history")
                    return [], None
            else:
                history_file = self.resolve_session_ref(str(session_ref))

            messages = self.read(history_file)
            logger.info(f"[session] resumed {history_file}")
            return messages, history_file
        except Exception as error:
            logger.warning(f"[session] resume failed: {error}")
            logger.info("[session] new reason=resume_failed")
            return [], None
        
    def save_session(self, session_path: Path | None, messages: list[dict]) -> Path:
        if session_path is None:
            return self.save(messages)
        return self.save_to(session_path, messages)


transcripts = TranscriptStore(TRANSCRIPT_DIR)
