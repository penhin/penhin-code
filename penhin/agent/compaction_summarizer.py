from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Protocol

from penhin.agent.projection import messages_for_api
from penhin.agent.prompts import AUTO_COMPACT_SYSTEM
from penhin.agent.session_store import serialize_value
from penhin.agent.token_accounting import estimate_message_tokens, estimate_text_tokens


MAX_SUMMARY_LEVELS = 8
SNAPSHOT_LIST_FIELDS = (
    "completed",
    "key_context",
    "constraints",
    "open_work",
    "tool_result_refs",
)
SNAPSHOT_TEXT_FIELDS = ("goal", "next_step")


class CompactionError(RuntimeError):
    pass


class SummaryBudget(Protocol):
    chunk_budget_tokens: int
    chunk_summary_max_tokens: int
    summary_max_tokens: int


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    snapshot: dict[str, Any]
    chunk_count: int
    calls: int
    levels: int


def is_tool_result_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def is_tool_use_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        (isinstance(block, dict) and block.get("type") == "tool_use")
        or getattr(block, "type", None) == "tool_use"
        for block in content
    )


def _atomic_message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        group = [messages[index]]
        if is_tool_use_message(messages[index]):
            cursor = index + 1
            while cursor < len(messages) and is_tool_result_message(messages[cursor]):
                group.append(messages[cursor])
                cursor += 1
            index = cursor
        else:
            index += 1
        groups.append(group)
    return groups


def _split_text_by_token_budget(text: str, budget_tokens: int) -> list[str]:
    if estimate_text_tokens(text) <= budget_tokens:
        return [text]
    max_units = max(1, budget_tokens) * 4
    fragments: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        char_units = 1 if ord(char) < 128 else 4
        if current and units + char_units > max_units:
            fragments.append("".join(current))
            current = []
            units = 0
        current.append(char)
        units += char_units
    if current:
        fragments.append("".join(current))
    return fragments


def chunk_compaction_messages(
    messages: list[dict[str, Any]],
    budget_tokens: int,
) -> list[list[dict[str, Any]]]:
    """Build bounded chunks while keeping normal tool call/result groups together."""
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for group in _atomic_message_groups(messages):
        group_tokens = sum(estimate_message_tokens(message) for message in group)
        if group_tokens > budget_tokens:
            if current:
                chunks.append(current)
                current = []
                current_tokens = 0
            serialized = json.dumps(serialize_value(group), ensure_ascii=False)
            fragments = _split_text_by_token_budget(serialized, budget_tokens)
            for index, fragment in enumerate(fragments, start=1):
                chunks.append([{
                    "role": "user",
                    "content": (
                        f"[Serialized message group fragment {index}/{len(fragments)}]\n"
                        f"{fragment}"
                    ),
                }])
            continue
        if current and current_tokens + group_tokens > budget_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.extend(copy.deepcopy(group))
        current_tokens += group_tokens
    if current:
        chunks.append(current)
    return chunks


def _summary_batches(summaries: list[str], budget_tokens: int) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for summary in summaries:
        summary_tokens = estimate_text_tokens(summary) + 8
        if current and current_tokens + summary_tokens > budget_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(summary)
        current_tokens += summary_tokens
    if current:
        batches.append(current)
    return batches


def _hint_section(hint: str | None) -> str:
    if not hint:
        return ""
    return (
        "User-provided compact hint:\n"
        f"{hint}\n\n"
        "Use this hint to decide what details deserve extra preservation.\n\n"
    )


def _summary_prompt(prefix: list[dict[str, Any]], hint: str | None) -> str:
    return (
        "Create a concise continuation snapshot from the covered conversation prefix. "
        "Return only a JSON object with exactly these fields: goal (non-empty string), "
        "completed (string array), key_context (string array), constraints (string array), "
        "open_work (string array), tool_result_refs (string array), next_step (non-empty string). "
        "Use tool_result_refs for important tool_use_id values; the runtime will verify them.\n"
        "Preserve the current goal, completed changes, concrete files/functions, constraints, "
        "tool evidence, unresolved risks, and next step. Write for the next agent turn. "
        "Do not summarize messages outside this covered prefix.\n\n"
        + _hint_section(hint)
        + json.dumps(serialize_value(messages_for_api(prefix)), ensure_ascii=False)
    )


def _chunk_summary_prompt(
    chunk: list[dict[str, Any]],
    index: int,
    total: int,
    hint: str | None,
) -> str:
    return (
        f"Summarize chronological conversation chunk {index}/{total}.\n"
        "Produce a dense, factual partial snapshot for later recursive merging. Preserve concrete "
        "goals, decisions, file/function names, tool evidence, errors, constraints, completed work, "
        "and unresolved items. Do not infer facts absent from this chunk.\n\n"
        + _hint_section(hint)
        + json.dumps(serialize_value(messages_for_api(chunk)), ensure_ascii=False)
    )


def _merge_summary_prompt(summaries: list[str], *, final: bool, hint: str | None) -> str:
    sections = "\n\n".join(
        f"<partial_summary index=\"{index}\">\n{summary}\n</partial_summary>"
        for index, summary in enumerate(summaries, start=1)
    )
    instruction = (
        "Merge these chronological partial summaries into one concise continuation snapshot. "
        "Resolve repetition without dropping later corrections or unresolved work. Write for the "
        "next agent turn. Return only a JSON object with exactly these fields: goal, completed, "
        "key_context, constraints, open_work, tool_result_refs, next_step. goal and next_step must be non-empty "
        "strings; the other fields must be string arrays."
        if final
        else "Merge these chronological partial summaries into a dense intermediate summary. "
        "Preserve later corrections and concrete technical details."
    )
    return instruction + "\n\n" + _hint_section(hint) + sections


def parse_continuation_snapshot(summary: str, max_tokens: int) -> tuple[str, dict[str, Any]]:
    text = summary.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise CompactionError(f"Compaction summary format was invalid JSON: {error.msg}") from error
    if not isinstance(value, dict) or set(value) != set(SNAPSHOT_TEXT_FIELDS + SNAPSHOT_LIST_FIELDS):
        raise CompactionError("Compaction summary format has missing or unexpected fields")
    snapshot: dict[str, Any] = {}
    for field in SNAPSHOT_TEXT_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise CompactionError(f"Compaction summary field {field} must be a non-empty string")
        snapshot[field] = item.strip()
    for field in SNAPSHOT_LIST_FIELDS:
        items = value.get(field)
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise CompactionError(f"Compaction summary field {field} must be a string array")
        snapshot[field] = [item.strip() for item in items]
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    actual_tokens = estimate_text_tokens(canonical)
    if actual_tokens > max_tokens:
        raise CompactionError(
            f"Compaction summary exceeded its token budget ({actual_tokens}/{max_tokens})"
        )
    return canonical, snapshot


def _call_summary(
    runtime: Any,
    prompt: str,
    max_tokens: int,
    stage: str,
    *,
    final: bool = False,
) -> str:
    try:
        summary = runtime.call_compact_once(
            system=AUTO_COMPACT_SYSTEM,
            user_content=prompt,
            max_tokens=max_tokens,
        )
    except Exception as error:
        raise CompactionError(f"Compaction {stage} failed: {error}") from error
    summary = str(summary or "").strip()
    if not summary:
        raise CompactionError(f"Compaction {stage} was empty")
    if final:
        summary, _snapshot = parse_continuation_snapshot(summary, max_tokens)
    return summary


def summarize_compaction_prefix(
    prefix: list[dict[str, Any]],
    decision: SummaryBudget,
    runtime: Any,
    hint: str | None,
) -> SummaryResult:
    chunks = chunk_compaction_messages(prefix, decision.chunk_budget_tokens)
    if len(chunks) == 1:
        summary = _call_summary(
            runtime,
            _summary_prompt(chunks[0], hint),
            decision.summary_max_tokens,
            "summary",
            final=True,
        )
        _canonical, snapshot = parse_continuation_snapshot(summary, decision.summary_max_tokens)
        return SummaryResult(summary, snapshot, chunk_count=1, calls=1, levels=1)

    summaries = [
        _call_summary(
            runtime,
            _chunk_summary_prompt(chunk, index, len(chunks), hint),
            decision.chunk_summary_max_tokens,
            f"chunk {index}/{len(chunks)} summary",
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    calls = len(summaries)
    levels = 1
    while len(summaries) > 1:
        if levels >= MAX_SUMMARY_LEVELS:
            raise CompactionError("Compaction summary exceeded the recursive merge level limit")
        batches = _summary_batches(summaries, decision.chunk_budget_tokens)
        if len(batches) == len(summaries):
            batches = [summaries[index:index + 4] for index in range(0, len(summaries), 4)]
        final_level = len(batches) == 1
        summaries = [
            _call_summary(
                runtime,
                _merge_summary_prompt(batch, final=final_level, hint=hint),
                decision.summary_max_tokens if final_level else decision.chunk_summary_max_tokens,
                "final merge" if final_level else f"merge level {levels + 1}",
                final=final_level,
            )
            for batch in batches
        ]
        calls += len(summaries)
        levels += 1
    canonical, snapshot = parse_continuation_snapshot(summaries[0], decision.summary_max_tokens)
    return SummaryResult(canonical, snapshot, len(chunks), calls, levels)


__all__ = [
    "CompactionError",
    "SummaryResult",
    "chunk_compaction_messages",
    "is_tool_result_message",
    "is_tool_use_message",
    "parse_continuation_snapshot",
    "summarize_compaction_prefix",
]
