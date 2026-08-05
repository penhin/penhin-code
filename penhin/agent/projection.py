from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Any


INTERNAL_META = "_meta"
PRESERVE_RESULT_TOOLS = {"todo_set", "todo_show", "todo_done", "todo_clear", "load_skill", "read"}
MIN_COLLAPSE_CHARS = 100


def block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def ensure_message_id(message: dict[str, Any]) -> str:
    meta = message.setdefault(INTERNAL_META, {})
    message_id = meta.get("id")
    if not isinstance(message_id, str) or not message_id:
        message_id = f"msg_{uuid.uuid4().hex}"
        meta["id"] = message_id
    return message_id


def message_meta(message: dict[str, Any]) -> dict[str, Any]:
    meta = message.get(INTERNAL_META)
    if not isinstance(meta, dict):
        meta = {}
        message[INTERNAL_META] = meta
    return meta


def mark_message_snipped(message: dict[str, Any], reason: str = "compact") -> None:
    message_id = ensure_message_id(message)
    meta = message_meta(message)
    meta["snipped"] = True
    meta["snip_reason"] = reason
    meta["snip_id"] = message_id


def is_snipped(message: dict[str, Any]) -> bool:
    meta = message.get(INTERNAL_META)
    return isinstance(meta, dict) and meta.get("snipped") is True


def tool_use_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    names = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block_value(block, "type") == "tool_use":
                tool_id = block_value(block, "id")
                tool_name = block_value(block, "name")
                if isinstance(tool_id, str) and isinstance(tool_name, str):
                    names[tool_id] = tool_name
    return names


def tool_result_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.append(block)
    return results


def dynamic_collapsed_content(
    block: dict[str, Any],
    tool_name: str,
) -> str:
    meta = block.get(INTERNAL_META)
    block_id = meta.get("id") if isinstance(meta, dict) else None
    if not isinstance(block_id, str) or not block_id:
        fingerprint = repr(block).encode("utf-8", errors="replace")
        block_id = f"block_{hashlib.sha1(fingerprint).hexdigest()[:12]}"
    content = block.get("content")
    original_chars = len(content) if isinstance(content, str) else 0
    return (
        f"[collapsed tool_result {tool_name}; reason=micro_compact; "
        f"original_chars={original_chars}; id={block_id}]"
    )


def dynamic_collapse_replacements(
    messages: list[dict[str, Any]],
    keep_recent: int | None,
) -> dict[int, str]:
    if keep_recent is None:
        return {}

    names_by_id = tool_use_names(messages)
    results = tool_result_blocks(messages)
    candidates = results if keep_recent <= 0 else results[:-keep_recent]
    replacements = {}
    for block in candidates:
        content = block.get("content")
        tool_id = block.get("tool_use_id")
        tool_name = names_by_id.get(tool_id, "unknown")
        if (
            isinstance(content, str)
            and len(content) > MIN_COLLAPSE_CHARS
            and tool_name not in PRESERVE_RESULT_TOOLS
        ):
            replacements[id(block)] = dynamic_collapsed_content(block, tool_name)
    return replacements


def project_block(block: Any, replacement: str | None = None) -> Any:
    if not isinstance(block, dict):
        return block

    projected = {
        key: copy.deepcopy(value)
        for key, value in block.items()
        if key != INTERNAL_META
    }
    if replacement is not None:
        projected["content"] = replacement
    return projected


def project_message(
    message: dict[str, Any],
    replacements: dict[int, str] | None = None,
) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(value)
        for key, value in message.items()
        if key != INTERNAL_META
    }

    content = message.get("content")
    if isinstance(content, list):
        projected_content = []
        for block in content:
            replacement = None
            if replacements is not None and isinstance(block, dict):
                replacement = replacements.get(id(block))
            projected_content.append(project_block(block, replacement))
        projected["content"] = projected_content
    return projected


def messages_for_api(
    messages: list[dict[str, Any]],
    collapse_keep_recent: int | None = None,
) -> list[dict[str, Any]]:
    visible_messages = [
        message for message in messages
        if not is_snipped(message)
    ]
    replacements = dynamic_collapse_replacements(visible_messages, collapse_keep_recent)
    return [
        project_message(message, replacements)
        for message in visible_messages
    ]
