import json
import logging
from typing import Any

from penhin.agent.projection import messages_for_api
from penhin.agent.prompts import AUTO_COMPACT_SYSTEM
from penhin.agent.token_accounting import estimate_context_tokens
from penhin.runtime import runtime_manager
from penhin.agent.session_store import serialize_value

SUMMARY_HEAD_CHARS = 40000
SUMMARY_TAIL_CHARS = 40000
KEEP_LAST_MESSAGES = 8

logger = logging.getLogger("penhin.compact")


def compact_source_text(messages: list[dict[str, Any]]) -> str:
    text = json.dumps(serialize_value(messages), ensure_ascii=False)
    max_chars = SUMMARY_HEAD_CHARS + SUMMARY_TAIL_CHARS
    if len(text) <= max_chars:
        return text
    return (
        text[:SUMMARY_HEAD_CHARS]
        + "\n...[middle omitted during compaction]...\n"
        + text[-SUMMARY_TAIL_CHARS:]
    )


class CompactionError(RuntimeError):
    pass


def estimate_api_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_context_tokens(messages).tokens


def compact_watermark(
    messages: list[dict[str, Any]],
    context_window: int,
    reserve_tokens: int,
) -> str:
    tokens = estimate_api_tokens(messages)
    compact_threshold = context_window - reserve_tokens
    if tokens >= context_window:
        return "blocking"
    if tokens >= compact_threshold:
        return "compact"
    if tokens >= int(compact_threshold * 0.75):
        return "warning"
    return "normal"


def log_compact_watermark(
    messages: list[dict[str, Any]],
    context_window: int,
    reserve_tokens: int,
) -> str:
    tokens = estimate_api_tokens(messages)
    compact_threshold = context_window - reserve_tokens
    watermark = compact_watermark(messages, context_window, reserve_tokens)
    if watermark == "warning":
        logger.warning(
            f"[compact] context above warning threshold "
            f"({tokens}/{compact_threshold})"
        )
    elif watermark == "compact":
        logger.warning(
            f"[compact] context above compact threshold "
            f"({tokens}/{compact_threshold}); auto compacting"
        )
    elif watermark == "blocking":
        logger.warning(
            f"[compact] context above blocking threshold "
            f"({tokens}/{context_window}); compact required"
        )
    return watermark


def is_tool_result_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def recent_message_start(messages: list[dict[str, Any]], keep_last: int) -> int:
    start = max(0, min(len(messages) - keep_last, len(messages) - 1))
    while start > 0 and is_tool_result_message(messages[start]):
        start -= 1
    return start


def safe_recent_messages(messages: list[dict[str, Any]], keep_last: int) -> list[dict[str, Any]]:
    if not messages:
        return []
    return messages[recent_message_start(messages, keep_last):]


def auto_compact_messages(
    messages: list[dict[str, Any]],
    keep_last: int = KEEP_LAST_MESSAGES,
    hint: str | None = None,
) -> list[dict[str, Any]]:
    conversation_text = compact_source_text(
        messages_for_api(messages)
    )
    hint_section = ""
    if hint:
        hint_section = (
            "User-provided compact hint:\n"
            f"{hint}\n\n"
            "Use this hint to decide what details deserve extra preservation.\n\n"
        )

    try:
        summary = runtime_manager.current().call_compact_once(
            system=AUTO_COMPACT_SYSTEM,
            user_content=(
                "Create a concise continuation snapshot from this conversation.\n"
                "Preserve these details when present:\n"
                "- Current user goal\n"
                "- Completed changes\n"
                "- Key files, functions, tools, and project structure\n"
                "- Important constraints and decisions\n"
                "- Open problems or risks\n"
                "- Recommended next step\n\n"
                "Write for the next agent turn, not for an end-user report. "
                "Prefer concrete file/function names over general summaries.\n\n"
                + hint_section
                + conversation_text
            ),
            max_tokens=2000,
        )
    except Exception as error:
        raise CompactionError(f"Compaction summary failed: {error}") from error

    if not summary:
        raise CompactionError("Compaction summary was empty")

    compacted = {
        "role": "user",
        "content": f"[Conversation compressed.]\n\n{summary}",
    }
    return [compacted] + safe_recent_messages(messages, keep_last)
    
