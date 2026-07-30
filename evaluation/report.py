from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from .io import read_json, write_json
from .metrics import orchestration_metrics_from_events, percentile, stability_by_case
from .observer import read_events


QUALITY_FIELDS = ("correctness", "relevance", "evidence", "maintainability")


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted((run_dir / "results").glob("*.json"))]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _quality(results: list[dict[str, Any]]) -> dict[str, float | None]:
    judges = [result["judge"] for result in results if result.get("judge")]
    return {field: mean(item[field] for item in judges) if judges else None for field in QUALITY_FIELDS}


def _dimensions(results: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    values = sorted({str(result.get(key, "unknown")) for result in results})
    return {
        value: {
            "runs": len(items := [item for item in results if str(item.get(key, "unknown")) == value]),
            "completion_rate": _rate(sum(bool(item.get("deterministic_passed")) for item in items), len(items)),
            "quality": _quality(items),
        }
        for value in values
    }


def _multi_agent_plan_modes(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    multi_agent = [item for item in results if item.get("layer") == "multi_agent"]
    modes = sorted({str(item.get("metrics", {}).get("orchestration_plan_mode", "unknown")) for item in multi_agent})
    return {
        mode: {
            "runs": len(items := [item for item in multi_agent if str(item.get("metrics", {}).get("orchestration_plan_mode", "unknown")) == mode]),
            "completion_rate": _rate(sum(bool(item.get("deterministic_passed")) for item in items), len(items)),
            "quality": _quality(items),
        }
        for mode in modes
    }


def _costs(manifest: dict[str, Any], events: list[dict[str, Any]], successful: int) -> dict[str, Any]:
    config = manifest["config"]
    prices = {"primary": config["primary_price"], "judge": config["judge_price"]}
    totals = {role: {"input_tokens": 0, "output_tokens": 0, "usd": 0.0} for role in prices}
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"llm_call_completed", "judge_call_usage"}:
            continue
        role = "judge" if event_type == "judge_call_usage" else "primary"
        usage = event.get("payload", {}).get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        price = prices[role]
        totals[role]["input_tokens"] += input_tokens
        totals[role]["output_tokens"] += output_tokens
        totals[role]["usd"] += (input_tokens * price["input_per_million"] + output_tokens * price["output_per_million"]) / 1_000_000
    total_usd = sum(item["usd"] for item in totals.values())
    for item in totals.values():
        item["usd"] = round(item["usd"], 8)
    budget_usd = float(manifest.get("budget", {}).get("used_usd", total_usd) or 0)
    return {
        "by_role": totals, "total_usd": round(total_usd, 8),
        "usd_per_success": round(total_usd / successful, 8) if successful else None,
        "budget_accounted_usd": round(budget_usd, 8),
        "accounting_delta_usd": round(budget_usd - total_usd, 8),
    }


def build_report(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    results = load_results(run_dir)
    events = read_events(run_dir)
    planned = int(manifest["planned_runs"])
    passed = sum(bool(result.get("deterministic_passed")) for result in results)
    safety = sum(len(result.get("safety_violations", [])) for result in results)
    statuses = Counter(str(result.get("status", "unknown")) for result in results)
    e2e = [float(result.get("metrics", {}).get("end_to_end_ms", 0) or 0) for result in results]
    costs = _costs(manifest, events, passed)
    llm_events = [event for event in events if event.get("event_type") == "llm_call_completed"]
    llm_failures = [event for event in events if event.get("event_type") == "llm_call_failed"]
    retry_events = [event for event in events if event.get("event_type") == "llm_retry"]
    tool_events = [event for event in events if event.get("event_type") == "tool_call_completed"]
    llm_latencies = [float(event.get("payload", {}).get("duration_ms", 0) or 0) for event in llm_events]
    tool_latencies = [float(event.get("payload", {}).get("duration_ms", 0) or 0) for event in tool_events]
    all_checks = [check for result in results for check in result.get("checks", [])]
    command_checks = [check for check in all_checks if str(check.get("name", "")).startswith("command:")]
    content_checks = [check for check in all_checks if str(check.get("name", "")).startswith("content:")]
    scope_checks = [check for check in all_checks if check.get("name") in {"allowed_paths", "forbidden_paths"}]
    stability = stability_by_case(results)
    stability.update({
        "timeout_rate": statuses.get("timed_out", 0) / planned if planned else 0.0,
        "provider_error_rate": len(llm_failures) / max(1, len(llm_events) + len(llm_failures)),
        "retry_recovery_rate": sum(int(event.get("payload", {}).get("attempt", 1) or 1) > 1 for event in llm_events) / max(1, len(retry_events)),
    })
    return {
        "schema_version": "penhin.eval.report/v1", "run_id": manifest["run_id"], "suite": manifest["suite"],
        "complete": manifest.get("status") == "complete" and len(results) == planned,
        "planned_runs": planned, "observed_runs": len(results), "task_completion_rate": _rate(passed, planned),
        "passed_runs": passed, "safety_violations": safety, "product_repository_unchanged": manifest.get("product_repository_unchanged", False),
        "statuses": dict(sorted(statuses.items())), "by_layer": _dimensions(results, "layer"), "by_category": _dimensions(results, "category"),
        "multi_agent_by_plan_mode": _multi_agent_plan_modes(results),
        "quality": _quality(results),
        "deterministic_quality": {
            "command_pass_rate": _rate(sum(bool(item.get("passed")) for item in command_checks), len(command_checks)),
            "content_pass_rate": _rate(sum(bool(item.get("passed")) for item in content_checks), len(content_checks)),
            "scope_pass_rate": _rate(sum(bool(item.get("passed")) for item in scope_checks), len(scope_checks)),
        },
        "stability": stability,
        "process": {
            "llm_calls": len(llm_events), "llm_retries": len(retry_events),
            "compactions": sum(event.get("event_type") == "context_compacted" for event in events),
            "mean_turns": mean(turns) if (turns := [float(item.get("metrics", {}).get("turns")) for item in results if item.get("metrics", {}).get("turns") is not None]) else None,
            "tests_executed": len(command_checks),
        },
        "latency": {
            "end_to_end_ms_p50": percentile(e2e, 0.5), "end_to_end_ms_p95": percentile(e2e, 0.95),
            "llm_ms_p50": percentile(llm_latencies, 0.5), "llm_ms_p95": percentile(llm_latencies, 0.95),
            "tool_ms_p50": percentile(tool_latencies, 0.5), "tool_ms_p95": percentile(tool_latencies, 0.95),
            "first_token_ms_p50": percentile([float(event.get("payload", {}).get("first_token_ms")) for event in llm_events if event.get("payload", {}).get("first_token_ms") is not None], 0.5),
        },
        "tools": {
            "calls": sum(int(item.get("metrics", {}).get("tool_calls", 0) or 0) for item in results),
            "failures": sum(round(float(item.get("metrics", {}).get("tool_failure_rate", 0) or 0) * int(item.get("metrics", {}).get("tool_calls", 0) or 0)) for item in results),
            "duplicate_calls": sum(int(item.get("metrics", {}).get("duplicate_tool_calls", 0) or 0) for item in results),
            "blocked_calls": sum(round(float(item.get("metrics", {}).get("tool_blocked_rate", 0) or 0) * int(item.get("metrics", {}).get("tool_calls", 0) or 0)) for item in results),
            "schema_warning_calls": sum(round(float(item.get("metrics", {}).get("tool_schema_warning_rate", 0) or 0) * int(item.get("metrics", {}).get("tool_calls", 0) or 0)) for item in results),
            "parallel_calls": sum(int(item.get("metrics", {}).get("parallel_tool_calls", 0) or 0) for item in results),
        },
        "orchestration": orchestration_metrics_from_events(events),
        "cost": costs, "budget": manifest.get("budget", {}),
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Penhin evaluation {report['run_id']}", "",
        f"- Complete: {report['complete']}",
        f"- Task completion: {report['passed_runs']}/{report['planned_runs']} ({report['task_completion_rate']:.1%})",
        f"- Safety violations: {report['safety_violations']}",
        f"- Product repository unchanged: {report['product_repository_unchanged']}",
        f"- Estimated cost: ${report['cost']['total_usd']:.4f}", "", "## Layers", "",
        "| Layer | Runs | Completion |", "|---|---:|---:|",
    ]
    for layer, data in report["by_layer"].items():
        lines.append(f"| {layer} | {data['runs']} | {data['completion_rate']:.1%} |")
    if report.get("multi_agent_by_plan_mode"):
        lines.extend(["", "## Multi-agent plan modes", "", "Fixture-driven recovery runs test orchestration mechanics and are reported separately from model-driven planning.", "", "| Plan mode | Runs | Completion |", "|---|---:|---:|"])
        for mode, data in report["multi_agent_by_plan_mode"].items():
            lines.append(f"| {mode} | {data['runs']} | {data['completion_rate']:.1%} |")
    lines.extend(["", "## Quality", ""])
    for field, value in report["quality"].items():
        lines.append(f"- {field}: {value:.2f}/5" if value is not None else f"- {field}: unavailable")
    lines.extend(["", "## Latency and tools", "", f"- End-to-end P50/P95: {report['latency']['end_to_end_ms_p50'] or 0:.0f}/{report['latency']['end_to_end_ms_p95'] or 0:.0f} ms", f"- Tool calls/failures/duplicates: {report['tools']['calls']}/{report['tools']['failures']}/{report['tools']['duplicate_calls']}"])
    orchestration = report.get("orchestration", {})
    lines.extend([
        "", "## Orchestration trace", "",
        f"- Plans started/validated/failed: {orchestration.get('plans_started', 0)}/{orchestration.get('plans_validated', 0)}/{orchestration.get('plans_failed', 0)}",
        f"- Jobs created/succeeded/failed: {orchestration.get('jobs_created', 0)}/{orchestration.get('jobs_succeeded', 0)}/{orchestration.get('jobs_failed', 0)}",
        f"- Invalid artifacts/integration conflicts: {orchestration.get('invalid_artifacts', 0)}/{orchestration.get('integration_conflicts', 0)}",
        f"- Failure stages: {json.dumps(orchestration.get('failure_stages', {}), ensure_ascii=False, sort_keys=True)}",
    ])
    return "\n".join(lines) + "\n"


def generate_report(run_dir: Path) -> dict[str, Any]:
    report = build_report(run_dir)
    write_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    return report


def compare_reports(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    overall_drop = baseline["task_completion_rate"] - current["task_completion_rate"]
    if overall_drop > 0.05:
        failures.append(f"overall completion dropped by {overall_drop:.1%}")
    for layer, baseline_layer in baseline["by_layer"].items():
        if layer in current["by_layer"]:
            drop = baseline_layer["completion_rate"] - current["by_layer"][layer]["completion_rate"]
            if drop > 0.10:
                failures.append(f"{layer} completion dropped by {drop:.1%}")
    for mode, baseline_mode in baseline.get("multi_agent_by_plan_mode", {}).items():
        current_mode = current.get("multi_agent_by_plan_mode", {}).get(mode)
        if current_mode is None:
            failures.append(f"multi-agent plan mode disappeared: {mode}")
            continue
        drop = baseline_mode["completion_rate"] - current_mode["completion_rate"]
        if drop > 0.10:
            failures.append(f"multi-agent {mode} completion dropped by {drop:.1%}")
    if current["safety_violations"] or not current["product_repository_unchanged"]:
        failures.append("safety invariant failed")
    baseline_bad = sum(baseline["statuses"].get(key, 0) for key in ("crashed", "timed_out", "failed")) / max(1, baseline["planned_runs"])
    current_bad = sum(current["statuses"].get(key, 0) for key in ("crashed", "timed_out", "failed")) / max(1, current["planned_runs"])
    if current_bad - baseline_bad > 0.03:
        failures.append(f"runtime failure rate increased by {current_bad - baseline_bad:.1%}")
    for field in QUALITY_FIELDS:
        old, new = baseline["quality"].get(field), current["quality"].get(field)
        if old is not None and new is not None and old - new > 0.3:
            warnings.append(f"judge {field} dropped by {old - new:.2f}")
    for field, label in (("end_to_end_ms_p95", "P95 latency"),):
        old, new = baseline["latency"].get(field), current["latency"].get(field)
        if old and new:
            growth = new / old - 1
            if growth > 0.4 and current["task_completion_rate"] <= baseline["task_completion_rate"]:
                failures.append(f"{label} increased by {growth:.1%} without completion improvement")
            elif growth > 0.2:
                warnings.append(f"{label} increased by {growth:.1%}")
    old_cost, new_cost = baseline["cost"]["total_usd"], current["cost"]["total_usd"]
    if old_cost:
        growth = new_cost / old_cost - 1
        if growth > 0.4 and current["task_completion_rate"] <= baseline["task_completion_rate"]:
            failures.append(f"cost increased by {growth:.1%} without completion improvement")
        elif growth > 0.2:
            warnings.append(f"cost increased by {growth:.1%}")
    return {"passed": not failures, "failures": failures, "warnings": warnings}
