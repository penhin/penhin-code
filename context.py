from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any

from compact import auto_compact_messages, log_compact_watermark, micro_compact_if_needed
from tool_runtime import ApprovalFlow, PermissionPolicy


@dataclass
class RunContext:
    messages: list[dict[str, Any]]
    policy: PermissionPolicy
    approval: ApprovalFlow
    session_path: Path | None = None

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: Any) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, tool_results: list[dict[str, Any]]) -> None:
        self.add_user_message(tool_results)

    def micro_compact(self) -> None:
        from compact import COMPACT_THRESHOLD
        micro_compact_if_needed(self.messages, limit=COMPACT_THRESHOLD)

    def auto_compact_if_needed(self) -> None:
        if log_compact_watermark(self.messages) in {"compact", "blocking"}:
            self.messages[:] = auto_compact_messages(self.messages)
            from tools.cache import tool_result_cache
            tool_result_cache.clear()

    def force_auto_compact(self) -> None:
        self.messages[:] = auto_compact_messages(self.messages)
        from tools.cache import tool_result_cache
        tool_result_cache.clear()
