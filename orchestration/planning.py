from __future__ import annotations

import json
import re
from typing import Any


DAG_PROTOCOL_VERSION = "penhin.dag/v1"
ALLOWED_AGENT_TYPES = {"explore", "general", "verification"}

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
Use `explore` for parallel read-only discovery, `general` only for isolated implementation, and `verification` after implementation. Dependencies must reference other job keys and form a DAG. Keep the graph minimal; never create a job merely to restate another job's output.
""".strip()


def _candidate(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1) if fenced else stripped


def parse_dag_plan(text: str) -> tuple[dict[str, Any], list[str]]:
    try:
        payload = json.loads(_candidate(text))
    except json.JSONDecodeError as error:
        return {}, [f"invalid JSON: {error.msg}"]
    errors = validate_dag_plan(payload)
    return payload if isinstance(payload, dict) else {}, errors


def validate_dag_plan(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["top-level value must be an object"]
    errors: list[str] = []
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
