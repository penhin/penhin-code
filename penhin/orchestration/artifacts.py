from __future__ import annotations

import json
from typing import Any


HANDOFF_PROTOCOL_VERSION = "penhin.handoff/v1"
HANDOFF_REQUIRED_FIELDS = {
    "protocol_version",
    "summary",
    "findings",
    "commands_run",
    "changed_files",
    "risks",
    "handoff",
}
SEVERITIES = {"info", "warning", "critical"}
COMMAND_OUTCOMES = {"passed", "failed", "blocked", "not_run"}
CHANGE_KINDS = {"created", "modified", "deleted", "none"}


def _string(value: Any, path: str, errors: list[str], required: bool = True) -> None:
    if not isinstance(value, str) or (required and not value.strip()):
        errors.append(f"{path} must be a{' non-empty' if required else ''} string")


def _list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array")
        return []
    return value


def validate_handoff(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["top-level value must be an object"]
    errors = [f"missing required field: {field}" for field in sorted(HANDOFF_REQUIRED_FIELDS - set(payload))]
    if payload.get("protocol_version") != HANDOFF_PROTOCOL_VERSION:
        errors.append(f"protocol_version must equal {HANDOFF_PROTOCOL_VERSION}")
    _string(payload.get("summary"), "summary", errors)
    for index, finding in enumerate(_list(payload.get("findings"), "findings", errors)):
        prefix = f"findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _string(finding.get("title"), f"{prefix}.title", errors)
        _string(finding.get("detail"), f"{prefix}.detail", errors)
        if finding.get("severity") not in SEVERITIES:
            errors.append(f"{prefix}.severity must be one of {sorted(SEVERITIES)}")
        for evidence_index, evidence in enumerate(_list(finding.get("evidence"), f"{prefix}.evidence", errors)):
            evidence_prefix = f"{prefix}.evidence[{evidence_index}]"
            if not isinstance(evidence, dict):
                errors.append(f"{evidence_prefix} must be an object")
                continue
            _string(evidence.get("path"), f"{evidence_prefix}.path", errors)
            _string(evidence.get("location"), f"{evidence_prefix}.location", errors)
            _string(evidence.get("detail"), f"{evidence_prefix}.detail", errors)
    for index, command in enumerate(_list(payload.get("commands_run"), "commands_run", errors)):
        prefix = f"commands_run[{index}]"
        if not isinstance(command, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _string(command.get("command"), f"{prefix}.command", errors)
        if command.get("outcome") not in COMMAND_OUTCOMES:
            errors.append(f"{prefix}.outcome must be one of {sorted(COMMAND_OUTCOMES)}")
        _string(command.get("detail"), f"{prefix}.detail", errors)
    for index, changed in enumerate(_list(payload.get("changed_files"), "changed_files", errors)):
        prefix = f"changed_files[{index}]"
        if not isinstance(changed, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _string(changed.get("path"), f"{prefix}.path", errors)
        if changed.get("change") not in CHANGE_KINDS:
            errors.append(f"{prefix}.change must be one of {sorted(CHANGE_KINDS)}")
        _string(changed.get("detail"), f"{prefix}.detail", errors)
    for index, risk in enumerate(_list(payload.get("risks"), "risks", errors)):
        prefix = f"risks[{index}]"
        if not isinstance(risk, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _string(risk.get("title"), f"{prefix}.title", errors)
        _string(risk.get("detail"), f"{prefix}.detail", errors)
        if risk.get("severity") not in SEVERITIES:
            errors.append(f"{prefix}.severity must be one of {sorted(SEVERITIES)}")
    handoff = payload.get("handoff")
    if not isinstance(handoff, dict):
        errors.append("handoff must be an object")
    else:
        _string(handoff.get("recommended_next_action"), "handoff.recommended_next_action", errors)
        _list(handoff.get("suggested_roles"), "handoff.suggested_roles", errors)
        _list(handoff.get("blocking_questions"), "handoff.blocking_questions", errors)
    return errors


def build_handoff(
    summary: str,
    *,
    producer: dict[str, str] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a valid handoff from runtime-owned data, not model-authored JSON."""
    report = summary.strip() or "Agent completed without a textual report."
    commands_run = []
    for tool_result in tool_results or []:
        name = str(tool_result.get("tool_name") or "unknown")
        try:
            result = json.loads(str(tool_result.get("content") or "{}"))
        except json.JSONDecodeError:
            result = {}
        ok = result.get("ok") is True
        detail = str(result.get("error") or result.get("message") or ("completed" if ok else "failed"))
        commands_run.append({
            "command": f"tool:{name}",
            "outcome": "passed" if ok else "failed",
            "detail": detail[:1000],
        })

    payload = {
        "protocol_version": HANDOFF_PROTOCOL_VERSION,
        "summary": report,
        "findings": [{
            "title": "Agent report",
            "detail": report,
            "severity": "info",
            "evidence": [],
        }],
        "commands_run": commands_run,
        "changed_files": [],
        "risks": [],
        "handoff": {
            "recommended_next_action": "Review the agent report and continue the task if needed.",
            "suggested_roles": [],
            "blocking_questions": [],
        },
        "protocol_valid": True,
        "protocol_errors": [],
        "producer": producer or {},
        "raw_text": summary,
    }
    errors = validate_handoff(payload)
    if errors:
        raise ValueError(f"Runtime-generated handoff is invalid: {errors}")
    return payload
