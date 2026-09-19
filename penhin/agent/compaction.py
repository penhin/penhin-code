from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from penhin.agent.compaction_summarizer import (
    CompactionError,
    chunk_compaction_messages,
    is_tool_result_message,
    is_tool_use_message,
    summarize_compaction_prefix,
)
from penhin.agent.projection import messages_for_api
from penhin.agent.prompts import is_project_instructions_message
from penhin.agent.session_store import serialize_value
from penhin.agent.token_accounting import (
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
)
from penhin.runtime import runtime_manager


DEFAULT_SUMMARY_MAX_TOKENS = 4_096
MAX_COMPACTION_PASSES = 3
MAX_TOOL_RESULT_REFS = 100
COMPACTION_ARTIFACT_VERSION = 2

logger = logging.getLogger("penhin.compact")


@dataclass(frozen=True)
class CompactionDecision:
    watermark: str
    reason: str
    should_compact: bool
    current_tokens: int
    context_window: int
    compact_threshold: int
    target_tokens: int
    tail_budget_tokens: int
    summary_max_tokens: int
    chunk_budget_tokens: int
    chunk_summary_max_tokens: int


class CompactionPolicy:
    """Pure policy boundary for deciding when and how much to compact."""

    def __init__(
        self,
        context_window: int,
        reserve_tokens: int,
        *,
        warning_ratio: float = 0.75,
        summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    ):
        if context_window <= 0:
            raise ValueError("context_window must be positive")
        if reserve_tokens <= 0 or reserve_tokens >= context_window:
            raise ValueError("reserve_tokens must be positive and smaller than context_window")
        if not 0 < warning_ratio < 1:
            raise ValueError("warning_ratio must be between zero and one")
        if summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens must be positive")
        self.context_window = context_window
        self.reserve_tokens = reserve_tokens
        self.warning_ratio = warning_ratio
        self.summary_max_tokens = summary_max_tokens

    def evaluate(
        self,
        messages: list[dict[str, Any]],
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> CompactionDecision:
        current_tokens = estimate_api_tokens(messages)
        compact_threshold = self.context_window - self.reserve_tokens
        if current_tokens >= self.context_window:
            watermark = "blocking"
        elif current_tokens >= compact_threshold:
            watermark = "compact"
        elif current_tokens >= int(compact_threshold * self.warning_ratio):
            watermark = "warning"
        else:
            watermark = "normal"

        summary_hard_limit = min(
            self.summary_max_tokens,
            max(1, self.reserve_tokens // 2),
            max(1, compact_threshold // 8),
        )
        summary_max_tokens = min(
            summary_hard_limit,
            max(128, self.context_window // 64),
        )
        chunk_budget_tokens = min(
            compact_threshold,
            max(summary_max_tokens * 4, min(32_000, compact_threshold // 2)),
        )
        chunk_summary_max_tokens = min(
            summary_max_tokens,
            max(32, chunk_budget_tokens // 8),
        )
        tail_budget_tokens = max(
            0,
            min(compact_threshold // 4, compact_threshold - summary_max_tokens),
        )
        should_compact = force or watermark in {"compact", "blocking"}
        return CompactionDecision(
            watermark=watermark,
            reason=reason or ("forced" if force else watermark),
            should_compact=should_compact,
            current_tokens=current_tokens,
            context_window=self.context_window,
            compact_threshold=compact_threshold,
            target_tokens=compact_threshold,
            tail_budget_tokens=tail_budget_tokens,
            summary_max_tokens=summary_max_tokens,
            chunk_budget_tokens=chunk_budget_tokens,
            chunk_summary_max_tokens=chunk_summary_max_tokens,
        )


@dataclass(frozen=True)
class ContextSelection:
    prefix: list[dict[str, Any]]
    tail: list[dict[str, Any]]
    split_index: int
    prefix_tokens: int
    tail_tokens: int


@dataclass(frozen=True)
class CompactionArtifact:
    summary: str
    snapshot: dict[str, Any]
    retained_messages: list[dict[str, Any]]
    source_hash: str
    covered_hash: str
    covered_message_count: int
    source_message_count: int
    model: str
    provider: str
    reason: str
    hint: str | None
    context_tokens: int
    source_tokens: int
    covered_tokens: int
    retained_tokens: int
    result_tokens: int
    target_tokens: int
    summary_max_tokens: int
    passes: int
    chunk_count: int
    summary_calls: int
    summary_levels: int
    limit_reached: bool
    tool_result_refs: list[dict[str, Any]]
    tool_result_refs_truncated: bool
    summary_tokens: int
    duration_ms: float

    def context_messages(self) -> list[dict[str, Any]]:
        return [compaction_summary_message(self.summary)] + copy.deepcopy(self.retained_messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": COMPACTION_ARTIFACT_VERSION,
            "summary": self.summary,
            "snapshot": copy.deepcopy(self.snapshot),
            "retainedMessages": copy.deepcopy(self.retained_messages),
            "sourceHash": self.source_hash,
            "coveredHash": self.covered_hash,
            "coveredMessageCount": self.covered_message_count,
            "sourceMessageCount": self.source_message_count,
            "model": self.model,
            "provider": self.provider,
            "reason": self.reason,
            "hint": self.hint,
            "tokenUsage": {
                "context": self.context_tokens,
                "source": self.source_tokens,
                "covered": self.covered_tokens,
                "retained": self.retained_tokens,
                "result": self.result_tokens,
                "target": self.target_tokens,
                "summaryMax": self.summary_max_tokens,
            },
            "strategy": {
                "passes": self.passes,
                "chunkCount": self.chunk_count,
                "summaryCalls": self.summary_calls,
                "summaryLevels": self.summary_levels,
                "recursive": self.summary_levels > self.passes,
                "limitReached": self.limit_reached,
            },
            "toolResultRefs": copy.deepcopy(self.tool_result_refs),
            "toolResultRefsTruncated": self.tool_result_refs_truncated,
            "quality": {
                "validated": True,
                "summaryTokens": self.summary_tokens,
                "compressionRatio": (
                    round(self.result_tokens / self.source_tokens, 6)
                    if self.source_tokens > 0
                    else 0.0
                ),
                "durationMs": round(self.duration_ms, 3),
                "toolResultRefCount": len(self.tool_result_refs),
            },
        }


@dataclass(frozen=True)
class PreparedCompaction:
    messages: list[dict[str, Any]]
    artifact: CompactionArtifact
    decision: CompactionDecision
    selection: ContextSelection


class CompactionCommitter:
    """Persist a prepared checkpoint before changing the live context."""

    @staticmethod
    def commit(
        target_messages: list[dict[str, Any]],
        prepared: PreparedCompaction,
        session_manager: Any = None,
    ) -> str | None:
        entry_id = None
        if session_manager is not None:
            try:
                entry_id = session_manager.append_compaction(prepared.artifact.to_dict())
            except Exception as error:
                raise CompactionError(f"Compaction checkpoint commit failed: {error}") from error
        target_messages[:] = copy.deepcopy(prepared.messages)
        return entry_id


def estimate_api_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_context_tokens(messages).tokens


def log_compact_decision(decision: CompactionDecision) -> None:
    if decision.watermark == "warning":
        logger.warning(
            f"[compact] context above warning threshold "
            f"({decision.current_tokens}/{decision.compact_threshold})"
        )
    elif decision.watermark == "compact":
        logger.warning(
            f"[compact] context above compact threshold "
            f"({decision.current_tokens}/{decision.compact_threshold}); auto compacting"
        )
    elif decision.watermark == "blocking":
        logger.warning(
            f"[compact] context above blocking threshold "
            f"({decision.current_tokens}/{decision.context_window}); compact required"
        )


def _safe_tail_start(messages: list[dict[str, Any]], start: int) -> int:
    if start <= 0 or start >= len(messages) or not is_tool_result_message(messages[start]):
        return start
    cursor = start - 1
    while cursor >= 0 and is_tool_result_message(messages[cursor]):
        cursor -= 1
    if cursor >= 0 and is_tool_use_message(messages[cursor]):
        return cursor
    return start


def select_compaction_context(
    messages: list[dict[str, Any]],
    tail_budget_tokens: int,
) -> ContextSelection:
    compactable = [
        message for message in messages
        if not is_project_instructions_message(message)
    ]
    if not compactable:
        raise CompactionError("No conversation messages are available to compact")

    start = len(compactable)
    tail_tokens = 0
    for index in range(len(compactable) - 1, -1, -1):
        message_tokens = estimate_message_tokens(compactable[index])
        if start < len(compactable) and tail_tokens + message_tokens > tail_budget_tokens:
            break
        if start == len(compactable) and message_tokens > tail_budget_tokens:
            break
        start = index
        tail_tokens += message_tokens

    start = _safe_tail_start(compactable, start)
    if start == 0:
        candidate = _safe_tail_start(compactable, 1) if len(compactable) > 1 else len(compactable)
        start = candidate if candidate > 0 else len(compactable)

    prefix = copy.deepcopy(compactable[:start])
    tail = copy.deepcopy(compactable[start:])
    prefix_tokens = sum(estimate_message_tokens(message) for message in prefix)
    tail_tokens = sum(estimate_message_tokens(message) for message in tail)
    return ContextSelection(prefix, tail, start, prefix_tokens, tail_tokens)


def _canonical_hash(messages: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        serialize_value(messages_for_api(messages)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_result_references(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    references: list[dict[str, Any]] = []
    truncated = False
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw_content = block.get("content")
            if not isinstance(raw_content, str):
                continue
            digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
            tool_use_id = str(block.get("tool_use_id", "") or "")
            recovery: dict[str, Any] = {}
            try:
                result_data = json.loads(raw_content)
            except json.JSONDecodeError:
                result_data = None
            if isinstance(result_data, dict):
                data = result_data.get("data")
                if isinstance(data, dict):
                    for key in ("path", "offset", "next_offset", "snapshot_id", "evidence_ref", "query", "command"):
                        value = data.get(key)
                        if isinstance(value, (str, int)):
                            recovery[key] = value
            references.append({
                "id": f"tool-result:{tool_use_id or 'unknown'}:{digest[:12]}",
                "toolUseId": tool_use_id,
                "toolName": str(block.get("tool_name", "") or ""),
                "messageIndex": message_index,
                "contentHash": digest,
                "contentChars": len(raw_content),
                "recovery": recovery,
            })
            if len(references) >= MAX_TOOL_RESULT_REFS:
                truncated = True
                return references, truncated
    return references, truncated


def _tool_result_reference_label(reference: dict[str, Any]) -> str:
    parts = [str(reference["id"])]
    tool_name = reference.get("toolName")
    if tool_name:
        parts.append(f"tool={tool_name}")
    recovery = reference.get("recovery")
    if isinstance(recovery, dict):
        for key in ("path", "offset", "next_offset", "snapshot_id", "evidence_ref", "query"):
            if key in recovery:
                parts.append(f"{key}={recovery[key]}")
    return " | ".join(parts)


def _without_usage_baselines(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = copy.deepcopy(messages)
    for message in cleaned:
        meta = message.get("_meta")
        if not isinstance(meta, dict):
            continue
        meta.pop("usage", None)
        if not meta:
            message.pop("_meta", None)
    return cleaned


def compaction_summary_message(summary: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": f"[Conversation compressed.]\n\n{summary}",
    }


def prepare_compaction(
    messages: list[dict[str, Any]],
    decision: CompactionDecision,
    *,
    hint: str | None = None,
    runtime: Any = None,
) -> PreparedCompaction:
    started = time.perf_counter()
    if not decision.should_compact:
        raise CompactionError("Compaction policy did not request compaction")
    active_runtime = runtime or runtime_manager.current()
    source_messages = [
        copy.deepcopy(message) for message in messages
        if not is_project_instructions_message(message)
    ]
    working_messages = copy.deepcopy(source_messages)
    summary = ""
    snapshot: dict[str, Any] = {}
    total_chunks = 0
    total_calls = 0
    total_levels = 0
    passes = 0
    limit_reached = False

    for pass_index in range(MAX_COMPACTION_PASSES):
        selection = select_compaction_context(
            working_messages,
            decision.tail_budget_tokens,
        )
        summary_result = summarize_compaction_prefix(
            selection.prefix,
            decision,
            active_runtime,
            hint,
        )
        summary = summary_result.summary
        snapshot = summary_result.snapshot
        retained = _without_usage_baselines(selection.tail)
        working_messages = [compaction_summary_message(summary)] + retained
        passes = pass_index + 1
        total_chunks += summary_result.chunk_count
        total_calls += summary_result.calls
        total_levels += summary_result.levels
        result_tokens = sum(estimate_message_tokens(message) for message in working_messages)
        if result_tokens < decision.target_tokens:
            break
    else:
        limit_reached = True

    retained = working_messages[1:]
    covered_count = len(source_messages) - len(retained)
    if covered_count <= 0:
        raise CompactionError("Compaction did not cover any source messages")
    covered_source = source_messages[:covered_count]
    tool_result_refs, tool_result_refs_truncated = _tool_result_references(covered_source)
    snapshot = copy.deepcopy(snapshot)
    snapshot["tool_result_refs"] = [
        _tool_result_reference_label(reference) for reference in tool_result_refs
    ]
    while tool_result_refs:
        summary = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        candidate_result_tokens = (
            estimate_message_tokens(compaction_summary_message(summary))
            + sum(estimate_message_tokens(message) for message in retained)
        )
        if (
            estimate_text_tokens(summary) <= decision.summary_max_tokens
            and (limit_reached or candidate_result_tokens < decision.target_tokens)
        ):
            break
        tool_result_refs.pop()
        tool_result_refs_truncated = True
        snapshot["tool_result_refs"] = [
            _tool_result_reference_label(reference) for reference in tool_result_refs
        ]
    summary = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if estimate_text_tokens(summary) > decision.summary_max_tokens:
        raise CompactionError("Compaction summary exceeded its token budget after adding recovery references")
    selection = ContextSelection(
        prefix=copy.deepcopy(covered_source),
        tail=copy.deepcopy(retained),
        split_index=covered_count,
        prefix_tokens=sum(estimate_message_tokens(message) for message in covered_source),
        tail_tokens=sum(estimate_message_tokens(message) for message in retained),
    )
    compacted = [compaction_summary_message(summary)] + copy.deepcopy(retained)
    result_tokens = sum(estimate_message_tokens(message) for message in compacted)
    source_tokens = sum(estimate_message_tokens(message) for message in source_messages)
    if result_tokens >= decision.target_tokens and result_tokens >= source_tokens:
        raise CompactionError("Compaction did not reduce the source context")
    artifact = CompactionArtifact(
        summary=summary,
        snapshot=snapshot,
        retained_messages=retained,
        source_hash=_canonical_hash(source_messages),
        covered_hash=_canonical_hash(covered_source),
        covered_message_count=covered_count,
        source_message_count=len(source_messages),
        model=str(getattr(active_runtime, "model", "") or ""),
        provider=str(getattr(active_runtime, "provider_id", "") or ""),
        reason=decision.reason,
        hint=hint,
        context_tokens=decision.current_tokens,
        source_tokens=source_tokens,
        covered_tokens=selection.prefix_tokens,
        retained_tokens=selection.tail_tokens,
        result_tokens=result_tokens,
        target_tokens=decision.target_tokens,
        summary_max_tokens=decision.summary_max_tokens,
        passes=passes,
        chunk_count=total_chunks,
        summary_calls=total_calls,
        summary_levels=total_levels,
        limit_reached=limit_reached and result_tokens >= decision.target_tokens,
        tool_result_refs=tool_result_refs,
        tool_result_refs_truncated=tool_result_refs_truncated,
        summary_tokens=estimate_text_tokens(summary),
        duration_ms=(time.perf_counter() - started) * 1_000,
    )
    return PreparedCompaction(compacted, artifact, decision, selection)


__all__ = [
    "COMPACTION_ARTIFACT_VERSION",
    "CompactionArtifact",
    "CompactionCommitter",
    "CompactionDecision",
    "CompactionError",
    "CompactionPolicy",
    "ContextSelection",
    "PreparedCompaction",
    "chunk_compaction_messages",
    "compaction_summary_message",
    "estimate_api_tokens",
    "log_compact_decision",
    "prepare_compaction",
    "select_compaction_context",
    "summarize_compaction_prefix",
]
