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


def project_block(block: Any) -> Any:
    if not isinstance(block, dict):
        return block

    projected = {
        key: copy.deepcopy(value)
        for key, value in block.items()
        if key != INTERNAL_META
    }
    return projected


def project_message(message: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(value)
        for key, value in message.items()
        if key != INTERNAL_META
    }

    content = message.get("content")
    if isinstance(content, list):
        projected_content = []
        for block in content:
            projected_content.append(project_block(block))
        projected["content"] = projected_content
    return projected


def messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible_messages = [
        message for message in messages
        if not is_snipped(message)
    ]
    return [
        project_message(message)
        for message in visible_messages
    ]
