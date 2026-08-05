from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from penhin.agent.prompts import PROJECT_INSTRUCTIONS_TAG
from penhin.agent.session_manager import SessionManager

SESSION_DIR = Path(".penhin/sessions")


def serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return serialize_value(value.model_dump(mode="json"))
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
    name: str = ""


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
        return (
            content.startswith("[Conversation compressed.")
            or content.strip().startswith(f"<{PROJECT_INSTRUCTIONS_TAG}>")
        )

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

    ok = result.get("ok")
    return not ok if isinstance(ok, bool) else False


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
    if path.name.startswith("session_"):
        return path.stem.removeprefix("session_")
    return path.stem


def normalize_session_ref(session_ref: str) -> str:
    name = Path(session_ref).name
    if name.endswith(".jsonl"):
        name = Path(name).stem
    if name.startswith("session_"):
        name = name.removeprefix("session_")
    return name


class SessionStore:
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self._managers: dict[Path, SessionManager] = {}

    def _remember(self, manager: SessionManager) -> SessionManager:
        self._managers[manager.path.resolve()] = manager
        return manager

    def _validate_path(self, path: Path) -> Path:
        resolved = path.resolve()
        base = self.session_dir.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Session path escapes session directory: {path}")
        if resolved.suffix != ".jsonl":
            raise ValueError(f"Session path must be a .jsonl file: {path}")
        return resolved

    def new(
        self,
        messages: list[dict[str, Any]] | None = None,
        *,
        parent_session: str | None = None,
    ) -> SessionManager:
        return self._remember(
            SessionManager.create(
                self.session_dir,
                [serialize_value(message) for message in messages or []],
                parent_session=parent_session,
            )
        )

    def open(self, path: Path) -> SessionManager:
        resolved = self._validate_path(path)
        manager = self._managers.get(resolved)
        if manager is None:
            manager = self._remember(SessionManager.open(path))
        return manager

    def resume(self, session_ref: Path | str | None = None) -> SessionManager:
        path = self.latest() if session_ref is None else self.resolve(str(session_ref))
        return self.open(path) if path is not None else self.new()

    def resolve(self, session_ref: str) -> Path:
        raw_path = Path(session_ref)
        candidates = [
            raw_path,
            self.session_dir / raw_path,
            self.session_dir / f"session_{session_ref}.jsonl",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        prefix_match = self.resolve_prefix(session_ref)
        if prefix_match is not None:
            return prefix_match

        raise FileNotFoundError(f"Session not found: {session_ref}")

    def resolve_prefix(self, session_ref: str) -> Path | None:
        if not self.session_dir.exists():
            return None

        normalized = normalize_session_ref(session_ref)
        if not normalized:
            return None

        matches = [
            path
            for path in self.session_dir.glob("session_*.jsonl")
            if session_id_from_path(path).startswith(normalized)
        ]
        if not matches:
            return None

        matches.sort(key=lambda path: (len(session_id_from_path(path)), path.stat().st_mtime), reverse=True)
        return matches[0]

    def latest(self) -> Path | None:
        if not self.session_dir.exists():
            return None

        paths = list(self.session_dir.glob("session_*.jsonl"))
        return max(paths, key=lambda path: path.stat().st_mtime_ns) if paths else None

    def list(self) -> list[SessionSummary]:
        if not self.session_dir.exists():
            return []

        summaries = []
        for path in sorted(self.session_dir.glob("session_*.jsonl")):
            try:
                manager = self.open(path)
                messages = manager.build_context()
            except Exception:
                continue

            summaries.append(
                SessionSummary(
                    id=session_id_from_path(path),
                    path=path,
                    updated_at=path.stat().st_mtime,
                    message_count=len(messages),
                    first_user=first_message_summary(messages, "user"),
                    name=manager.get_session_name(),
                )
            )
        return summaries

    def inspect(self, session_ref: str, event_limit: int = 8) -> SessionInspect:
        path = self.resolve(session_ref)
        messages = self.open(path).build_context()
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


sessions = SessionStore(SESSION_DIR)


__all__ = ["SessionInspect", "SessionStore", "SessionSummary", "serialize_value", "sessions"]
