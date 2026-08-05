from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from penhin.auth.secrets import safe_value
from penhin.infrastructure.atomic_io import read_jsonl, write_jsonl_atomic


SESSION_VERSION = 1
SESSION_TYPE = "session"


class SessionFormatError(ValueError):
    pass


def _entry_id() -> str:
    return uuid4().hex[:12]


def _timestamp() -> str:
    milliseconds = int(time.time() * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(milliseconds / 1000)) + f".{milliseconds % 1000:03d}Z"


def _serialized(value: Any) -> str:
    return json.dumps(safe_value(value), ensure_ascii=False, sort_keys=True, default=str)


def _persistent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message for message in messages
        if not (
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].strip().startswith("<project_instructions>")
        )
    ]


class SessionManager:
    """Append-only JSONL session tree with an in-memory active leaf."""

    def __init__(self, path: Path, header: dict[str, Any], entries: list[dict[str, Any]]):
        self.path = path
        self.header = header
        self.entries = entries
        self._lock = threading.RLock()
        self._by_id: dict[str, dict[str, Any]] = {}
        self._children: dict[str | None, list[str]] = {}
        self._index_entries()
        self.leaf_id = entries[-1]["id"] if entries else None

    @classmethod
    def create(
        cls,
        session_dir: Path,
        messages: list[dict[str, Any]] | None = None,
        *,
        cwd: Path | None = None,
        parent_session: str | None = None,
    ) -> SessionManager:
        session_dir.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid4())
        path = session_dir / f"session_{session_id}.jsonl"
        header: dict[str, Any] = {
            "type": SESSION_TYPE,
            "version": SESSION_VERSION,
            "id": session_id,
            "timestamp": _timestamp(),
            "cwd": str((cwd or Path.cwd()).resolve()),
        }
        if parent_session:
            header["parentSession"] = parent_session
        write_jsonl_atomic(path, [safe_value(header)])
        manager = cls(path, header, [])
        manager.append_messages(messages or [])
        return manager

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        items = read_jsonl(path)
        if not items:
            raise SessionFormatError(f"Empty session file: {path}")
        if not isinstance(items[0], dict) or items[0].get("type") != SESSION_TYPE:
            raise SessionFormatError(f"Invalid session header: {path}")
        header = items[0]
        version = header.get("version")
        if version != SESSION_VERSION:
            raise SessionFormatError(f"Unsupported session version: {version}")
        entries = items[1:]
        if not all(isinstance(entry, dict) for entry in entries):
            raise SessionFormatError(f"Invalid session entry in {path}")
        return cls(path, header, entries)

    @property
    def id(self) -> str:
        return str(self.header["id"])

    def _index_entries(self) -> None:
        for entry in self.entries:
            entry_id = entry.get("id")
            parent_id = entry.get("parentId")
            if not isinstance(entry_id, str) or not entry_id:
                raise SessionFormatError("Session entry is missing an id")
            if entry_id in self._by_id:
                raise SessionFormatError(f"Duplicate session entry id: {entry_id}")
            if parent_id is not None and parent_id not in self._by_id:
                raise SessionFormatError(f"Unknown parentId {parent_id!r} for entry {entry_id}")
            self._by_id[entry_id] = entry
            self._children.setdefault(parent_id, []).append(entry_id)

    def _append_line(self, entry: dict[str, Any]) -> None:
        encoded = json.dumps(safe_value(entry), ensure_ascii=False) + "\n"
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def append_entry(self, entry_type: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            entry = {
                "type": entry_type,
                "id": _entry_id(),
                "parentId": self.leaf_id,
                "timestamp": _timestamp(),
                **safe_value(payload),
            }
            self._append_line(entry)
            self.entries.append(entry)
            self._by_id[entry["id"]] = entry
            self._children.setdefault(self.leaf_id, []).append(entry["id"])
            self.leaf_id = entry["id"]
            return entry

    def append_message(self, message: dict[str, Any]) -> str:
        return str(self.append_entry("message", message=message)["id"])

    def append_messages(self, messages: list[dict[str, Any]]) -> None:
        for message in _persistent_messages(messages):
            self.append_message(message)

    def append_compaction(self, messages: list[dict[str, Any]], reason: str = "compact") -> str:
        return str(self.append_entry("compaction", messages=_persistent_messages(messages), reason=reason)["id"])

    def append_session_info(self, name: str) -> str:
        return str(self.append_entry("session_info", name=name)["id"])

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        return self._by_id.get(entry_id)

    def resolve_entry_id(self, reference: str) -> str:
        if reference in self._by_id:
            return reference
        matches = [entry_id for entry_id in self._by_id if entry_id.startswith(reference)]
        if not matches:
            raise KeyError(f"Session entry not found: {reference}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous session entry prefix: {reference}")
        return matches[0]

    def children(self, parent_id: str | None) -> list[dict[str, Any]]:
        return [self._by_id[entry_id] for entry_id in self._children.get(parent_id, [])]

    def branch(self, entry_id: str | None) -> list[dict[str, Any]]:
        if entry_id is not None and entry_id not in self._by_id:
            raise KeyError(f"Session entry not found: {entry_id}")
        self.leaf_id = entry_id
        return self.build_context()

    def branch_entries(self, leaf_id: str | None = None) -> list[dict[str, Any]]:
        current = self.leaf_id if leaf_id is None else leaf_id
        branch: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise SessionFormatError(f"Cycle in session tree at {current}")
            seen.add(current)
            entry = self._by_id.get(current)
            if entry is None:
                raise SessionFormatError(f"Missing session entry: {current}")
            branch.append(entry)
            current = entry.get("parentId")
        branch.reverse()
        return branch

    def build_context(self, leaf_id: str | None = None) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for entry in self.branch_entries(leaf_id):
            if entry["type"] == "message":
                message = entry.get("message")
                if isinstance(message, dict):
                    messages.append(copy.deepcopy(message))
            elif entry["type"] == "compaction":
                compacted = entry.get("messages")
                if isinstance(compacted, list):
                    messages = [copy.deepcopy(message) for message in compacted if isinstance(message, dict)]
        return messages

    def sync_messages(self, messages: list[dict[str, Any]]) -> None:
        messages = _persistent_messages(messages)
        current = self.build_context()
        current_keys = [_serialized(message) for message in current]
        desired_keys = [_serialized(message) for message in messages]
        if current_keys == desired_keys:
            return
        if desired_keys[:len(current_keys)] == current_keys:
            self.append_messages(messages[len(current):])
            return
        self.append_compaction(messages, reason="context_rewrite")

    def get_session_name(self) -> str:
        name = ""
        for entry in self.branch_entries():
            if entry["type"] == "session_info" and isinstance(entry.get("name"), str):
                name = entry["name"]
        return name

    def fork(self, session_dir: Path, entry_id: str | None = None) -> SessionManager:
        target = self.leaf_id if entry_id is None else entry_id
        if target is not None and target not in self._by_id:
            raise KeyError(f"Session entry not found: {target}")
        return SessionManager.create(
            session_dir,
            self.build_context(target),
            parent_session=str(self.path),
        )

    def render_tree(self) -> list[str]:
        lines: list[str] = []

        def visit(parent_id: str | None, prefix: str) -> None:
            children = self.children(parent_id)
            for index, entry in enumerate(children):
                last = index == len(children) - 1
                connector = "└─" if last else "├─"
                marker = " *" if entry["id"] == self.leaf_id else ""
                lines.append(f"{prefix}{connector} {entry['id']} {self._entry_summary(entry)}{marker}")
                visit(entry["id"], prefix + ("   " if last else "│  "))

        visit(None, "")
        return lines

    @staticmethod
    def _entry_summary(entry: dict[str, Any], limit: int = 72) -> str:
        entry_type = str(entry.get("type", "unknown"))
        if entry_type == "message":
            message = entry.get("message", {})
            role = str(message.get("role", "message")) if isinstance(message, dict) else "message"
            content = message.get("content", "") if isinstance(message, dict) else ""
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        value = block.get("text") or block.get("content") or block.get("name")
                        if isinstance(value, str):
                            parts.append(value)
                text = " ".join(parts)
            else:
                text = str(content)
            text = " ".join(text.split())
            return f"{role}: {text[:limit]}"
        if entry_type == "compaction":
            return f"compaction ({entry.get('reason', 'compact')})"
        if entry_type == "session_info":
            return f"name: {entry.get('name', '')}"
        return entry_type


__all__ = ["SESSION_VERSION", "SessionFormatError", "SessionManager"]
