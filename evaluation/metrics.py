from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from statistics import mean, pstdev
from typing import Any


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def metrics_from_events(events: list[dict[str, Any]], expected_tools: tuple[str, ...] = ()) -> dict[str, Any]:
    llm = [e for e in events if e.get("event_type") == "llm_call_completed"]
    tools = [e for e in events if e.get("event_type") == "tool_call_completed"]
    retries = [e for e in events if e.get("event_type") == "llm_retry"]
    compactions = [e for e in events if e.get("event_type") == "context_compacted"]
    parallel_batches = [e for e in events if e.get("event_type") == "parallel_tool_batch_started"]
    run_starts = [e for e in events if e.get("event_type") == "agent_run_started"]
    run_ends = [e for e in events if e.get("event_type") == "agent_run_completed"]
    tool_names = [str(e.get("payload", {}).get("tool_name", "")) for e in tools]
    normalized = [(name, str(e.get("payload", {}).get("input_digest", ""))) for name, e in zip(tool_names, tools)]
    duplicates = sum(count - 1 for count in Counter(normalized).values() if count > 1)
    input_tokens = sum(int(e.get("payload", {}).get("usage", {}).get("input_tokens", 0) or 0) for e in llm)
    output_tokens = sum(int(e.get("payload", {}).get("usage", {}).get("output_tokens", 0) or 0) for e in llm)
    llm_ms = [float(e.get("payload", {}).get("duration_ms", 0) or 0) for e in llm]
    tool_ms = [float(e.get("payload", {}).get("duration_ms", 0) or 0) for e in tools]
    successful = sum(e.get("payload", {}).get("status") == "ok" for e in tools)
    failed = sum(e.get("payload", {}).get("status") == "error" for e in tools)
    blocked = sum(e.get("payload", {}).get("status") in {"blocked", "approval_required"} for e in tools)
    schema_warnings = sum(bool(e.get("payload", {}).get("unknown_input_fields")) for e in tools)
    first_tool_ms = None
    if run_starts and tools:
        first_tool_ms = max(0.0, (tools[0].get("monotonic_ns", 0) - run_starts[0].get("monotonic_ns", 0)) / 1_000_000)
    queue_ms = []
    for event in events:
        if event.get("event_type") != "orchestration_job_claimed":
            continue
        payload = event.get("payload", {})
        try:
            created = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
            started = datetime.fromisoformat(str(payload["started_at"]).replace("Z", "+00:00"))
            queue_ms.append((started - created).total_seconds() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
    expected_found = len(set(expected_tools) & set(tool_names))
    return {
        "llm_calls": len(llm), "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens, "retries": len(retries), "compactions": len(compactions),
        "tool_calls": len(tools), "tool_success_rate": successful / len(tools) if tools else None,
        "tool_failure_rate": failed / len(tools) if tools else None,
        "tool_blocked_rate": blocked / len(tools) if tools else None,
        "tool_schema_warning_rate": schema_warnings / len(tools) if tools else None,
        "duplicate_tool_calls": duplicates,
        "parallel_tool_calls": sum(int(event.get("payload", {}).get("call_count", 0) or 0) for event in parallel_batches),
        "parallel_batch_count": len(parallel_batches),
        "expected_tool_coverage": expected_found / len(expected_tools) if expected_tools else None,
        "time_to_first_tool_ms": first_tool_ms,
        "queue_time_ms_p50": percentile(queue_ms, 0.5), "queue_time_ms_p95": percentile(queue_ms, 0.95),
        "turns": run_ends[-1].get("payload", {}).get("turns") if run_ends else None,
        "terminal_reason": run_ends[-1].get("payload", {}).get("terminal_reason") if run_ends else None,
        "llm_latency_ms_p50": percentile(llm_ms, 0.5), "llm_latency_ms_p95": percentile(llm_ms, 0.95),
        "tool_latency_ms_p50": percentile(tool_ms, 0.5), "tool_latency_ms_p95": percentile(tool_ms, 0.95),
        "first_token_ms_p50": percentile([float(e.get("payload", {}).get("first_token_ms")) for e in llm if e.get("payload", {}).get("first_token_ms") is not None], 0.5),
    }


def stability_by_case(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["case_id"], []).append(result)
    mixed = 0
    all_pass = 0
    judge_deviations = []
    for items in grouped.values():
        outcomes = {bool(item.get("deterministic_passed")) for item in items}
        mixed += len(outcomes) > 1
        all_pass += bool(items) and all(item.get("deterministic_passed") for item in items)
        scores = [mean([j[k] for k in ("correctness", "relevance", "evidence", "maintainability")]) for item in items if (j := item.get("judge"))]
        if len(scores) > 1:
            judge_deviations.append(pstdev(scores))
    count = len(grouped)
    return {
        "case_count": count,
        "all_repetitions_pass_rate": all_pass / count if count else 0.0,
        "mixed_outcome_case_rate": mixed / count if count else 0.0,
        "mean_judge_score_stddev": mean(judge_deviations) if judge_deviations else None,
    }
