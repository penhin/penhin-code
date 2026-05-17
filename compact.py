import json
from runtime import get_runtime
from transcript import serialize_for_json, transcripts

THRESHOLD = 50000
MAX_CHARS = 50000
SUMMARY_HEAD_CHARS = 40000
SUMMARY_TAIL_CHARS = 40000
KEEP_RECENT = 3
KEEP_LAST_MESSAGES = 8
PRESERVE_RESULT_TOOLS = {"todo_set", "todo_show", "todo_done", "todo_clear", "load_skill", "read"}


def block_get(block, key: str, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def compact_source_text(messages: list[dict]) -> str:
    text = json.dumps(serialize_for_json(messages), ensure_ascii=False)
    max_chars = SUMMARY_HEAD_CHARS + SUMMARY_TAIL_CHARS
    if len(text) <= max_chars:
        return text
    return (
        text[:SUMMARY_HEAD_CHARS]
        + "\n...[middle omitted during compaction]...\n"
        + text[-SUMMARY_TAIL_CHARS:]
    )


def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4


def micro_compact_text(messages: list[dict], keep_recent: int = KEEP_RECENT) -> None:
    tool_results = []
    for msg_id, msg in enumerate(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_id, part_idx, part))
                    
    tool_name_map = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block_get(block, "type") == "tool_use":
                        tool_name_map[block_get(block, "id")] = block_get(block, "name")
    
    to_clear = tool_results if keep_recent <= 0 else tool_results[:-keep_recent]
    for _, _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"


def should_auto_compact(messages: list[dict], threshold: int = THRESHOLD) -> bool:
    return estimate_tokens(messages) >= threshold


def is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def recent_message_start(messages: list[dict], keep_last: int) -> int:
    start = max(0, min(len(messages) - keep_last, len(messages) - 1))
    while start > 0 and is_tool_result_message(messages[start]):
        start -= 1
    return start


def auto_compact_messages(
    messages: list[dict],
    keep_last: int = KEEP_LAST_MESSAGES,
) -> list[dict]:
    transcript_path = transcripts.save(messages)

    print(f"[transcript saved: {transcript_path}]")

    conversation_text = compact_source_text(messages)
    try:
        summary = get_runtime().call_llm_once(
            system=(
                "You summarize coding-agent conversation history into a compact "
                "state snapshot for continuing engineering work."
            ),
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
                + conversation_text
            ),
            max_tokens=2000,
            label="compact",
        )
    except Exception as error:
        summary = f"Summary failed during compaction: {error}"

    if not summary:
        summary = "No summary generated."

    start = recent_message_start(messages, keep_last)
    compacted = {
        "role": "user",
        "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
    }
    return [compacted] + messages[start:]
    
