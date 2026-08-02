from __future__ import annotations

from typing import Any

from .metrics import orchestration_metrics_from_events


def build_trace_summary(
    events: list[dict[str, Any]],
    *,
    case_id: str,
    repetition: int,
) -> dict[str, Any]:
    selected = [
        event for event in events
        if event.get("case_id") == case_id and int(event.get("repetition", 0) or 0) == repetition
    ]
    orchestration = [
        event for event in selected
        if str(event.get("event_type", "")).startswith(("orchestration_", "integration_"))
    ]
    origin = min((int(event.get("monotonic_ns", 0) or 0) for event in selected), default=0)
    timeline = []
    for event in orchestration:
        correlation = event.get("correlation", {})
        payload = event.get("payload", {})
        timeline.append({
            "offset_ms": round((int(event.get("monotonic_ns", 0) or 0) - origin) / 1_000_000, 3) if origin else None,
            "event_type": event.get("event_type"),
            "event_id": event.get("event_id"),
            "trace_id": correlation.get("trace_id"),
            "root_task_id": payload.get("root_task_id") or correlation.get("root_task_id"),
            "job_id": payload.get("job_id") or correlation.get("job_id"),
            "attempt_id": payload.get("attempt_id") or correlation.get("attempt_id"),
            "stage": payload.get("stage"),
            "status": payload.get("status"),
            "error_code": payload.get("error_code"),
            "protocol_errors": payload.get("protocol_errors", []),
            "artifact_id": payload.get("artifact_id"),
            "integration_id": payload.get("integration_id"),
        })
    metrics = orchestration_metrics_from_events(selected)
    diagnostics = []
    for stage, count in metrics["failure_stages"].items():
        diagnostics.append({"kind": "failure_stage", "stage": stage, "count": count})
    for code, count in metrics["error_codes"].items():
        diagnostics.append({"kind": "error_code", "code": code, "count": count})
    if metrics["dangling_job_ids"]:
        diagnostics.append({"kind": "dangling_jobs", "job_ids": metrics["dangling_job_ids"]})
    if metrics["invalid_artifacts"]:
        diagnostics.append({"kind": "invalid_artifacts", "count": metrics["invalid_artifacts"]})
    root_cause = None
    for item in timeline:
        if item.get("error_code") and item.get("error_code") not in {"failed", "worker_exit"}:
            root_cause = {
                "event_type": item["event_type"], "stage": item.get("stage"),
                "error_code": item["error_code"], "job_id": item.get("job_id"),
                "attempt_id": item.get("attempt_id"), "protocol_errors": item.get("protocol_errors", []),
            }
            break
    return {
        "schema_version": "penhin.eval.trace/v1",
        "case_id": case_id,
        "repetition": repetition,
        "event_count": len(selected),
        "orchestration_event_count": len(orchestration),
        "metrics": metrics,
        "root_cause": root_cause,
        "diagnostics": diagnostics,
        "timeline": timeline,
    }
