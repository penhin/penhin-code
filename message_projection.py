from __future__ import annotations

import copy
import uuid
from typing import Any


INTERNAL_META = "_meta"


def ensure_message_id(message: dict[str, Any]) -> str:
    meta = message.setdefault(INTERNAL_META, {})
    message_id = meta.get("id")
    if not isinstance(message_id, str) or not message_id:
        message_id = f"msg_{uuid.uuid4().hex}"
        meta["id"] = message_id
    return message_id


def ensure_block_id(block: dict[str, Any]) -> str:
    meta = block.setdefault(INTERNAL_META, {})
    block_id = meta.get("id")
    if not isinstance(block_id, str) or not block_id:
        block_id = f"block_{uuid.uuid4().hex}"
        meta["id"] = block_id
    return block_id


def message_meta(message: dict[str, Any]) -> dict[str, Any]:
    meta = message.get(INTERNAL_META)
    if not isinstance(meta, dict):
        meta = {}
        message[INTERNAL_META] = meta
    return meta


def block_meta(block: dict[str, Any]) -> dict[str, Any]:
    meta = block.get(INTERNAL_META)
    if not isinstance(meta, dict):
        meta = {}
        block[INTERNAL_META] = meta
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


def mark_block_collapsed(block: dict[str, Any], label: str, reason: str = "micro_compact") -> None:
    block_id = ensure_block_id(block)
    content = block.get("content")
    original_chars = len(content) if isinstance(content, str) else 0
    meta = block_meta(block)
    meta["collapse"] = {
        "id": block_id,
        "label": label,
        "reason": reason,
        "original_chars": original_chars,
    }


def collapsed_content(block: dict[str, Any]) -> str | None:
    meta = block.get(INTERNAL_META)
    if not isinstance(meta, dict):
        return None
    collapse = meta.get("collapse")
    if not isinstance(collapse, dict):
        return None

    label = collapse.get("label", "tool result")
    reason = collapse.get("reason", "collapse")
    original_chars = collapse.get("original_chars", 0)
    collapse_id = collapse.get("id", "-")
    return (
        f"[collapsed {label}; reason={reason}; "
        f"original_chars={original_chars}; id={collapse_id}]"
    )


def project_block(block: Any) -> Any:
    if not isinstance(block, dict):
        return block

    projected = {
        key: copy.deepcopy(value)
        for key, value in block.items()
        if key != INTERNAL_META
    }
    replacement = collapsed_content(block)
    if replacement is not None:
        projected["content"] = replacement
    return projected


def project_message(message: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(value)
        for key, value in message.items()
        if key != INTERNAL_META
    }

    content = message.get("content")
    if isinstance(content, list):
        projected["content"] = [
            project_block(block)
            for block in content
        ]
    return projected


def messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        project_message(message)
        for message in messages
        if not is_snipped(message)
    ]
