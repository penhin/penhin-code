from __future__ import annotations

import json
import re
from typing import Any


DAG_PROTOCOL_VERSION = "penhin.dag/v1"
ALLOWED_AGENT_TYPES = {"explore", "general", "verification"}
PLAN_KEYS = {"protocol_version", "goal", "jobs", "final_job_keys"}
JOB_KEYS = {"key", "agent_type", "instruction", "depends_on", "priority", "timeout_seconds"}


def dag_protocol_instructions() -> str:
    return """
Return one JSON object only, without Markdown fences or surrounding prose.
It must conform to penhin.dag/v1:
{
  "protocol_version": "penhin.dag/v1",
  "goal": "the requested outcome",
  "jobs": [
    {"key": "short-unique-key", "agent_type": "explore|general|verification", "instruction": "focused, self-contained assignment", "depends_on": [], "priority": 0}
  ],
  "final_job_keys": ["keys whose handoffs answer the goal"]
}
Use `explore` for parallel read-only discovery, `general` only when the user explicitly requests repository changes, and `verification` after implementation. Never use `general` for analysis, review, explanation, or planning-only requests. Every `general` job must feed a downstream `verification` job, and a downstream verification must be a final job. Dependencies must reference other job keys and form a DAG. Keep the graph minimal; never create a job merely to restate another job's output.
""".strip()


def _candidate(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1)
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            _, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return stripped[index:index + end]
    return stripped


def parse_dag_plan(text: str) -> tuple[dict[str, Any], list[str]]:
    try:
        payload = json.loads(_candidate(text))
    except json.JSONDecodeError as error:
        return {}, [f"invalid JSON: {error.msg}"]
    errors = validate_dag_plan(payload)
    return payload if isinstance(payload, dict) else {}, errors


def fallback_dag_plan(goal: str) -> dict[str, Any]:
    """Return a read-only degraded topology when model-authored JSON cannot be repaired.

    Natural-language write intent is deliberately not inferred here: a false
    positive would grant an implementation worker to a read-only request.
    """
    jobs = [
        {"key": "inspect", "agent_type": "explore", "instruction": f"Investigate and collect concrete evidence for: {goal}", "depends_on": [], "priority": 1},
        {"key": "verify", "agent_type": "verification", "instruction": f"Review the upstream evidence and report what can be concluded safely for: {goal}", "depends_on": ["inspect"], "priority": 0},
    ]
    return {"protocol_version": DAG_PROTOCOL_VERSION, "goal": goal, "jobs": jobs, "final_job_keys": ["verify"]}


def normalize_dag_plan(plan: dict[str, Any], goal: str) -> tuple[dict[str, Any], list[str]]:
    """Enforce topology invariants without guessing intent from user wording."""
    normalized = json.loads(json.dumps(plan))
    changes: list[str] = []
    jobs = normalized.get("jobs", [])
    general_keys = [job.get("key") for job in jobs if job.get("agent_type") == "general"]
    if general_keys:
        graph = {job.get("key"): job.get("depends_on", []) for job in jobs}

        def depends_on_general(key: str, seen: set[str] | None = None) -> bool:
            seen = seen or set()
            if key in seen:
                return False
            seen.add(key)
            dependencies = graph.get(key, [])
            return any(item in general_keys or depends_on_general(item, seen.copy()) for item in dependencies)

        verification_keys = [
            job.get("key") for job in jobs
            if job.get("agent_type") == "verification" and depends_on_general(str(job.get("key")))
        ]
        if not verification_keys:
            existing = {str(job.get("key")) for job in jobs}
            key = "verify"
            suffix = 2
            while key in existing:
                key, suffix = f"verify-{suffix}", suffix + 1
            dependencies = list(dict.fromkeys([*normalized.get("final_job_keys", []), *general_keys]))
            jobs.append({
                "key": key, "agent_type": "verification",
                "instruction": f"Independently verify the integrated implementation and run focused tests for: {goal}",
                "depends_on": dependencies, "priority": 0,
            })
            normalized["final_job_keys"] = [key]
            changes.append("added a final verification node downstream of implementation")
        elif not any(key in verification_keys for key in normalized.get("final_job_keys", [])):
            normalized["final_job_keys"] = [verification_keys[0]]
            changes.append("selected a downstream verification node as the final output")
    return normalized, changes


def validate_dag_plan(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["top-level value must be an object"]
    errors: list[str] = []
    unknown_top_level = sorted(set(payload) - PLAN_KEYS)
    if unknown_top_level:
        errors.append(f"unknown top-level fields: {', '.join(unknown_top_level)}")
    if payload.get("protocol_version") != DAG_PROTOCOL_VERSION:
        errors.append(f"protocol_version must equal {DAG_PROTOCOL_VERSION}")
    if not isinstance(payload.get("goal"), str) or not payload["goal"].strip():
        errors.append("goal must be a non-empty string")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return errors + ["jobs must be a non-empty array"]
    keys: set[str] = set()
    graph: dict[str, list[str]] = {}
    for index, job in enumerate(jobs):
        prefix = f"jobs[{index}]"
        if not isinstance(job, dict):
            errors.append(f"{prefix} must be an object")
            continue
        unknown_job_fields = sorted(set(job) - JOB_KEYS)
        if unknown_job_fields:
            errors.append(f"{prefix} has unknown fields: {', '.join(unknown_job_fields)}")
        key = job.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key):
            errors.append(f"{prefix}.key must be a lowercase slug")
            continue
        if key in keys:
            errors.append(f"duplicate job key: {key}")
        keys.add(key)
        graph[key] = job.get("depends_on", []) if isinstance(job.get("depends_on"), list) else []
        if job.get("agent_type") not in ALLOWED_AGENT_TYPES:
            errors.append(f"{prefix}.agent_type must be one of {sorted(ALLOWED_AGENT_TYPES)}")
        if not isinstance(job.get("instruction"), str) or not job["instruction"].strip():
            errors.append(f"{prefix}.instruction must be a non-empty string")
        if not isinstance(job.get("depends_on"), list) or not all(isinstance(item, str) for item in job.get("depends_on", [])):
            errors.append(f"{prefix}.depends_on must be an array of keys")
        if "priority" in job and (not isinstance(job["priority"], int) or isinstance(job["priority"], bool)):
            errors.append(f"{prefix}.priority must be an integer")
        if "timeout_seconds" in job and (
            not isinstance(job["timeout_seconds"], int)
            or isinstance(job["timeout_seconds"], bool)
            or not 1 <= job["timeout_seconds"] <= 3600
        ):
            errors.append(f"{prefix}.timeout_seconds must be an integer between 1 and 3600")
    for key, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in keys:
                errors.append(f"job {key} depends on unknown key {dependency}")
    if not errors and _has_cycle(graph):
        errors.append("job dependencies must be acyclic")
    final_keys = payload.get("final_job_keys")
    if not isinstance(final_keys, list) or not final_keys or not all(isinstance(key, str) and key in keys for key in final_keys):
        errors.append("final_job_keys must be a non-empty array of job keys")
    return errors


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> bool:
        if key in visiting:
            return True
        if key in visited:
            return False
        visiting.add(key)
        cycle = any(visit(dependency) for dependency in graph[key])
        visiting.remove(key)
        visited.add(key)
        return cycle

    return any(visit(key) for key in graph)
