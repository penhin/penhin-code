from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextTokenEstimate:
    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None
    source: str


def estimate_text_tokens(text: str) -> int:
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return math.ceil(ascii_chars / 4 + non_ascii_chars)


def _content_tokens(value: Any) -> int:
    if isinstance(value, str):
        return estimate_text_tokens(value)
    if isinstance(value, list):
        return sum(_content_tokens(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key == "_meta":
                continue
            if key in {"input", "arguments"}:
                total += estimate_text_tokens(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
            else:
                total += _content_tokens(item)
        return total
    if value is None:
        return 0
    return estimate_text_tokens(str(value))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    # Small envelope allowance covers role and provider message framing.
    return 4 + _content_tokens({key: value for key, value in message.items() if key != "_meta"})


def _usage_context_tokens(message: dict[str, Any]) -> int | None:
    if message.get("role") != "assistant":
        return None
    meta = message.get("_meta")
    if not isinstance(meta, dict):
        return None
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("context_tokens")
    return value if isinstance(value, int) and value > 0 else None


def _is_snipped(message: dict[str, Any]) -> bool:
    meta = message.get("_meta")
    return isinstance(meta, dict) and meta.get("snipped") is True


def estimate_trailing_tokens(messages: list[dict[str, Any]], start_index: int) -> int:
    return sum(
        estimate_message_tokens(message)
        for message in messages[start_index:]
        if not _is_snipped(message)
    )


def estimate_context_tokens(messages: list[dict[str, Any]]) -> ContextTokenEstimate:
    for index in range(len(messages) - 1, -1, -1):
        usage_tokens = _usage_context_tokens(messages[index])
        if usage_tokens is None:
            continue
        snipped_prefix_tokens = sum(
            estimate_message_tokens(message)
            for message in messages[: index + 1]
            if _is_snipped(message)
        )
        adjusted_usage_tokens = max(0, usage_tokens - snipped_prefix_tokens)
        trailing = estimate_trailing_tokens(messages, index + 1)
        return ContextTokenEstimate(
            tokens=adjusted_usage_tokens + trailing,
            usage_tokens=adjusted_usage_tokens,
            trailing_tokens=trailing,
            last_usage_index=index,
            source="provider_usage+trailing_estimate",
        )

    estimated = estimate_trailing_tokens(messages, 0)
    return ContextTokenEstimate(
        tokens=estimated,
        usage_tokens=0,
        trailing_tokens=estimated,
        last_usage_index=None,
        source="message_estimate",
    )


__all__ = [
    "ContextTokenEstimate",
    "estimate_context_tokens",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "estimate_trailing_tokens",
]
