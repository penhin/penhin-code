from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any

from compact import auto_compact_messages, log_compact_watermark, micro_compact_if_needed
from message_projection import mark_message_snipped
from tool_runtime import ApprovalFlow, PermissionPolicy


@dataclass
class RunContext:
    messages: list[dict[str, Any]]
    policy: PermissionPolicy
    approval: ApprovalFlow
    session_path: Path | None = None
    collapse_keep_recent: int | None = None

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: Any) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, tool_results: list[dict[str, Any]]) -> None:
        self.add_user_message(tool_results)

    def micro_compact(self) -> None:
        from compact import COMPACT_THRESHOLD
        self.collapse_keep_recent = micro_compact_if_needed(self.messages, limit=COMPACT_THRESHOLD)

    def auto_compact_if_needed(self) -> None:
        if log_compact_watermark(self.messages, self.collapse_keep_recent) in {"compact", "blocking"}:
            self.messages[:] = auto_compact_messages(
                self.messages,
                collapse_keep_recent=self.collapse_keep_recent,
            )
            from tools.cache import tool_result_cache
            tool_result_cache.clear()

    def force_auto_compact(self, hint: str | None = None) -> None:
        self.messages[:] = auto_compact_messages(
            self.messages,
            hint=hint,
            collapse_keep_recent=self.collapse_keep_recent,
        )
        from tools.cache import tool_result_cache
        tool_result_cache.clear()

    def force_snip_turns(self, selectors: list[int | tuple[int, int]]) -> int:
        ranges = conversation_turn_ranges(self.messages)
        selected = selected_turn_numbers(selectors)
        snipped = 0
        for turn_number, start, end, _summary in ranges:
            if turn_number not in selected:
                continue
            for message in self.messages[start:end]:
                mark_message_snipped(message, reason="force_snip")
                snipped += 1
        if snipped:
            from tools.cache import tool_result_cache
            tool_result_cache.clear()
        return snipped


def is_human_user_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return True
    return not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def message_summary(message: dict[str, Any], limit: int = 80) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        text = str(content)

    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def conversation_turn_ranges(messages: list[dict[str, Any]]) -> list[tuple[int, int, int, str]]:
    starts = [
        index for index, message in enumerate(messages)
        if is_human_user_message(message)
    ]
    ranges = []
    for offset, start in enumerate(starts):
        end = starts[offset + 1] if offset + 1 < len(starts) else len(messages)
        ranges.append((offset + 1, start, end, message_summary(messages[start])))
    return ranges


def selected_turn_numbers(selectors: list[int | tuple[int, int]]) -> set[int]:
    selected = set()
    for selector in selectors:
        if isinstance(selector, int):
            selected.add(selector)
        else:
            start, end = selector
            low, high = sorted((start, end))
            selected.update(range(low, high + 1))
    return selected


def parse_snip_selectors(args: list[str]) -> list[int | tuple[int, int]]:
    selectors = []
    for arg in args:
        if "-" in arg:
            start_text, end_text = arg.split("-", 1)
            selectors.append((int(start_text), int(end_text)))
        else:
            selectors.append(int(arg))
    return selectors
