from __future__ import annotations

import copy
import hashlib
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
SNAPSHOT_TEXT_FIELDS = {"goal", "next_step"}
SNAPSHOT_LIST_FIELDS = {
    "completed", "key_context", "constraints", "open_work", "tool_result_refs",
}


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


def _valid_snapshot_payload(summary: Any, snapshot: Any) -> bool:
    if not isinstance(summary, str) or not isinstance(snapshot, dict):
        return False
    if set(snapshot) != SNAPSHOT_TEXT_FIELDS | SNAPSHOT_LIST_FIELDS:
        return False
    if not all(isinstance(snapshot[field], str) and snapshot[field].strip() for field in SNAPSHOT_TEXT_FIELDS):
        return False
    if not all(
        isinstance(snapshot[field], list)
        and all(isinstance(item, str) and item.strip() for item in snapshot[field])
        for field in SNAPSHOT_LIST_FIELDS
    ):
        return False
    try:
        parsed_summary = json.loads(summary)
    except json.JSONDecodeError:
        return False
    return parsed_summary == snapshot


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

    def append_compaction(self, artifact: dict[str, Any]) -> str:
        artifact = copy.deepcopy(artifact)
        if artifact.get("version") not in {1, 2}:
            raise ValueError("Unsupported compaction artifact version")
        if not isinstance(artifact.get("summary"), str) or not artifact["summary"].strip():
            raise ValueError("Compaction artifact requires a summary")
        retained = artifact.get("retainedMessages")
        if not isinstance(retained, list) or not all(isinstance(message, dict) for message in retained):
            raise ValueError("Compaction artifact retainedMessages must be a message list")
        if artifact.get("version") == 2 and not _valid_snapshot_payload(
            artifact.get("summary"), artifact.get("snapshot")
        ):
            raise ValueError("Version-2 compaction artifact requires a valid structured snapshot")

        _messages, source_ids = self._build_context_state()
        source_count = int(artifact.get("sourceMessageCount", -1))
        covered_count = int(artifact.get("coveredMessageCount", 0) or 0)
        if source_count != len(source_ids):
            raise ValueError("Compaction artifact sourceMessageCount does not match the active branch")
        if covered_count <= 0 or covered_count > source_count:
            raise ValueError("Compaction artifact coveredMessageCount is outside the active branch")
        if len(retained) != source_count - covered_count:
            raise ValueError("Compaction artifact retainedMessages does not match its coverage")
        artifact["sourceLeafId"] = self.leaf_id
        artifact["coveredThroughEntryId"] = (
            source_ids[covered_count - 1]
            if 0 < covered_count <= len(source_ids)
            else None
        )
        artifact["retainedSourceEntryIds"] = source_ids[covered_count:]
        references = artifact.get("toolResultRefs")
        if references is not None:
            if not isinstance(references, list) or not all(isinstance(reference, dict) for reference in references):
                raise ValueError("Compaction artifact toolResultRefs must be an object list")
            for reference in references:
                message_index = reference.get("messageIndex")
                if not isinstance(message_index, int) or not 0 <= message_index < covered_count:
                    raise ValueError("Compaction tool result reference is outside the covered prefix")
                reference["sourceEntryId"] = source_ids[message_index]
        return str(self.append_entry("compaction", artifact=artifact)["id"])

    def append_context_rewrite(self, messages: list[dict[str, Any]], reason: str) -> str:
        return str(self.append_entry(
            "context_rewrite",
            messages=_persistent_messages(messages),
            reason=reason,
        )["id"])

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

    def _build_context_state(
        self,
        leaf_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        messages: list[dict[str, Any]] = []
        source_ids: list[str] = []
        for entry in self.branch_entries(leaf_id):
            if entry["type"] == "message":
                message = entry.get("message")
                if isinstance(message, dict):
                    messages.append(copy.deepcopy(message))
                    source_ids.append(str(entry["id"]))
            elif entry["type"] == "compaction":
                artifact = entry.get("artifact")
                if self._valid_compaction_artifact(artifact, entry, messages, source_ids):
                    retained = artifact.get("retainedMessages")
                    messages = [{
                        "role": "user",
                        "content": f"[Conversation compressed.]\n\n{artifact['summary']}",
                    }] + [copy.deepcopy(message) for message in retained if isinstance(message, dict)]
                    retained_sources = artifact.get("retainedSourceEntryIds")
                    if not isinstance(retained_sources, list) or len(retained_sources) != len(messages) - 1:
                        retained_sources = [str(entry["id"])] * (len(messages) - 1)
                    source_ids = [str(entry["id"])] + [str(item) for item in retained_sources]
                else:
                    # Version-1 sessions used compaction entries as full context snapshots.
                    compacted = entry.get("messages")
                    if isinstance(compacted, list):
                        messages = [copy.deepcopy(message) for message in compacted if isinstance(message, dict)]
                        source_ids = [str(entry["id"])] * len(messages)
            elif entry["type"] == "context_rewrite":
                rewritten = entry.get("messages")
                if isinstance(rewritten, list):
                    messages = [copy.deepcopy(message) for message in rewritten if isinstance(message, dict)]
                    source_ids = [str(entry["id"])] * len(messages)
        return messages, source_ids

    @staticmethod
    def _valid_compaction_artifact(
        artifact: Any,
        entry: dict[str, Any],
        messages: list[dict[str, Any]],
        source_ids: list[str],
    ) -> bool:
        if not isinstance(artifact, dict) or artifact.get("version") not in {1, 2}:
            return False
        if not isinstance(artifact.get("summary"), str) or not artifact["summary"].strip():
            return False
        retained = artifact.get("retainedMessages")
        if not isinstance(retained, list) or not all(isinstance(message, dict) for message in retained):
            return False
        if artifact.get("version") == 2 and not _valid_snapshot_payload(
            artifact.get("summary"), artifact.get("snapshot")
        ):
            return False
        source_count = artifact.get("sourceMessageCount")
        covered_count = artifact.get("coveredMessageCount")
        if source_count != len(messages) or not isinstance(covered_count, int):
            return False
        if covered_count <= 0 or covered_count > source_count:
            return False
        if len(retained) != source_count - covered_count:
            return False
        source_leaf = artifact.get("sourceLeafId")
        if source_leaf is not None and source_leaf != entry.get("parentId"):
            return False
        covered_through = artifact.get("coveredThroughEntryId")
        if covered_through is not None and covered_through != source_ids[covered_count - 1]:
            return False
        retained_sources = artifact.get("retainedSourceEntryIds")
        if retained_sources is not None and retained_sources != source_ids[covered_count:]:
            return False
        if artifact.get("version") == 2:
            references = artifact.get("toolResultRefs")
            if not isinstance(references, list):
                return False
            for reference in references:
                if not isinstance(reference, dict):
                    return False
                message_index = reference.get("messageIndex")
                if not isinstance(message_index, int) or not 0 <= message_index < covered_count:
                    return False
                if reference.get("sourceEntryId") != source_ids[message_index]:
                    return False
        return True

    def build_context(self, leaf_id: str | None = None) -> list[dict[str, Any]]:
        messages, _source_ids = self._build_context_state(leaf_id)
        return messages

    def recover_tool_result(
        self,
        checkpoint_id: str,
        reference_id: str,
    ) -> dict[str, Any] | None:
        checkpoint = self.get_entry(self.resolve_entry_id(checkpoint_id))
        if checkpoint is None or checkpoint.get("type") != "compaction":
            return None
        artifact = checkpoint.get("artifact")
        references = artifact.get("toolResultRefs") if isinstance(artifact, dict) else None
        reference = next(
            (
                item for item in references or []
                if isinstance(item, dict) and item.get("id") == reference_id
            ),
            None,
        )
        if reference is None:
            return None
        expected_hash = reference.get("contentHash")
        expected_tool_use_id = reference.get("toolUseId")
        for entry in reversed(self.branch_entries(str(checkpoint["id"]))):
            for message in self._entry_context_messages(entry):
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    raw_content = block.get("content")
                    if not isinstance(raw_content, str):
                        continue
                    digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
                    if block.get("tool_use_id") == expected_tool_use_id and digest == expected_hash:
                        return copy.deepcopy(block)
        return None

    @staticmethod
    def _entry_context_messages(entry: dict[str, Any]) -> list[dict[str, Any]]:
        if entry.get("type") == "message" and isinstance(entry.get("message"), dict):
            return [entry["message"]]
        if entry.get("type") == "context_rewrite" and isinstance(entry.get("messages"), list):
            return [message for message in entry["messages"] if isinstance(message, dict)]
        if entry.get("type") == "compaction":
            artifact = entry.get("artifact")
            if isinstance(artifact, dict) and isinstance(artifact.get("retainedMessages"), list):
                return [message for message in artifact["retainedMessages"] if isinstance(message, dict)]
            if isinstance(entry.get("messages"), list):
                return [message for message in entry["messages"] if isinstance(message, dict)]
        return []

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
        self.append_context_rewrite(messages, reason="context_rewrite")

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
        forked = SessionManager.create(
            session_dir,
            self.build_context(target),
            parent_session=str(self.path),
        )
        planning = next((entry for entry in reversed(self.branch_entries(target)) if entry["type"] == "planning"), None)
        if planning is not None:
            from dataclasses import asdict
            from penhin.agent.planning import PlanningState
            forked.append_entry("planning", state=asdict(PlanningState.from_entry(planning)))
        return forked

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
            artifact = entry.get("artifact")
            reason = artifact.get("reason", "compact") if isinstance(artifact, dict) else entry.get("reason", "compact")
            return f"compaction ({reason})"
        if entry_type == "context_rewrite":
            return f"context rewrite ({entry.get('reason', 'rewrite')})"
        if entry_type == "session_info":
            return f"name: {entry.get('name', '')}"
        return entry_type


__all__ = ["SESSION_VERSION", "SessionFormatError", "SessionManager"]
