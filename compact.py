import json
import logging
from typing import Any

from prompt import AUTO_COMPACT_SYSTEM
from runtime import get_runtime
from transcript import serialize_for_json, transcripts
from message_flow import block_get
from message_projection import mark_block_collapsed, mark_message_snipped, messages_for_api
from circuit_breaker import CircuitBreakerOpen

logger = logging.getLogger("penhin.compact")

WARNING_THRESHOLD = 80000
COMPACT_THRESHOLD = 120000
BLOCKING_THRESHOLD = 160000
THRESHOLD = COMPACT_THRESHOLD
SUMMARY_HEAD_CHARS = 40000
SUMMARY_TAIL_CHARS = 40000
KEEP_RECENT = 3
KEEP_HEAD_MESSAGES = 2
KEEP_LAST_MESSAGES = 8
PRESERVE_RESULT_TOOLS = {"todo_set", "todo_show", "todo_done", "todo_clear", "load_skill", "read"}
MIN_COLLAPSE_CHARS = 100


def compact_source_text(messages: list[dict[str, Any]]) -> str:
    text = json.dumps(serialize_for_json(messages), ensure_ascii=False)
    max_chars = SUMMARY_HEAD_CHARS + SUMMARY_TAIL_CHARS
    if len(text) <= max_chars:
        return text
    return (
        text[:SUMMARY_HEAD_CHARS]
        + "\n...[middle omitted during compaction]...\n"
        + text[-SUMMARY_TAIL_CHARS:]
    )


def estimate_tokens(messages: list[Any]) -> int:
    return len(str(messages)) // 4


def estimate_api_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_tokens(messages_for_api(messages))


def micro_compact_if_needed(messages: list[dict[str, Any]], limit: int = COMPACT_THRESHOLD) -> None:
    if limit <= 0:
        return

    tokens = estimate_tokens(messages)
    usage_ratio = tokens / limit

    if usage_ratio < 0.2:
        return
    elif usage_ratio < 0.6:
        micro_compact_text(messages, 5)
    elif usage_ratio < 0.8:
        micro_compact_text(messages, 3)
    else:
        micro_compact_text(messages, 1)


def tool_use_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    names = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block_get(block, "type") == "tool_use":
                names[block_get(block, "id")] = block_get(block, "name")
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


def old_tool_results(results: list[dict[str, Any]], keep_recent: int) -> list[dict[str, Any]]:
    if keep_recent <= 0:
        return results
    return results[:-keep_recent]


def should_collapse_tool_result(result: dict[str, Any], tool_name: str) -> bool:
    content = result.get("content")
    return (
        isinstance(content, str)
        and len(content) > MIN_COLLAPSE_CHARS
        and tool_name not in PRESERVE_RESULT_TOOLS
    )


def micro_compact_text(messages: list[dict[str, Any]], keep_recent: int = KEEP_RECENT) -> None:
    names_by_id = tool_use_names(messages)
    results = tool_result_blocks(messages)

    for result in old_tool_results(results, keep_recent):
        tool_id = result.get("tool_use_id", "")
        tool_name = names_by_id.get(tool_id, "unknown")
        if should_collapse_tool_result(result, tool_name):
            mark_block_collapsed(result, label=f"tool_result {tool_name}")


def compact_watermark(messages: list[dict[str, Any]]) -> str:
    tokens = estimate_api_tokens(messages)
    if tokens >= BLOCKING_THRESHOLD:
        return "blocking"
    if tokens >= COMPACT_THRESHOLD:
        return "compact"
    if tokens >= WARNING_THRESHOLD:
        return "warning"
    return "normal"


def log_compact_watermark(messages: list[dict[str, Any]]) -> str:
    watermark = compact_watermark(messages)
    if watermark == "warning":
        logger.warning(
            f"[compact] context above warning threshold "
            f"({estimate_api_tokens(messages)}/{WARNING_THRESHOLD})"
        )
    elif watermark == "compact":
        logger.warning(
            f"[compact] context above compact threshold "
            f"({estimate_api_tokens(messages)}/{COMPACT_THRESHOLD}); auto compacting"
        )
    elif watermark == "blocking":
        logger.warning(
            f"[compact] context above blocking threshold "
            f"({estimate_api_tokens(messages)}/{BLOCKING_THRESHOLD}); compact required"
        )
    return watermark


def should_auto_compact(messages: list[dict[str, Any]], threshold: int = COMPACT_THRESHOLD) -> bool:
    return estimate_api_tokens(messages) >= threshold


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


def auto_compact_messages(
    messages: list[dict[str, Any]],
    keep_head: int = KEEP_HEAD_MESSAGES,
    keep_last: int = KEEP_LAST_MESSAGES,
    hint: str | None = None,
) -> list[dict[str, Any]]:
    transcript_path = transcripts.save(messages)

    logger.info(f"[transcript saved: {transcript_path}]")

    conversation_text = compact_source_text(messages_for_api(messages))
    hint_section = ""
    if hint:
        hint_section = (
            "User-provided compact hint:\n"
            f"{hint}\n\n"
            "Use this hint to decide what details deserve extra preservation.\n\n"
        )

    try:
        summary = get_runtime().call_compact_once(
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
    except CircuitBreakerOpen as error:
        summary = f"Summary skipped during compaction: compact circuit breaker is open ({error})"
    except Exception as error:
        summary = f"Summary failed during compaction: {error}"

    if not summary:
        summary = "No summary generated."

    head_end = max(0, min(keep_head, len(messages)))
    start = max(head_end, recent_message_start(messages, keep_last))
    for message in messages[head_end:start]:
        mark_message_snipped(message, reason="auto_compact")

    compacted = {
        "role": "user",
        "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
    }
    return [compacted] + messages
    
