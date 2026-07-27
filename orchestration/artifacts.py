from __future__ import annotations

import json
import re
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


def collaboration_protocol_instructions() -> str:
    return """
Return the final handoff as one JSON object only, without Markdown fences or surrounding prose.
It must conform to penhin.handoff/v1:
{
  "protocol_version": "penhin.handoff/v1",
  "summary": "non-empty concise conclusion",
  "findings": [{"title": "finding", "detail": "evidence-backed detail", "severity": "info|warning|critical", "evidence": [{"path": "relative/path", "location": "line or symbol", "detail": "what proves it"}]}],
  "commands_run": [{"command": "exact command", "outcome": "passed|failed|blocked|not_run", "detail": "observed result"}],
  "changed_files": [{"path": "relative/path", "change": "created|modified|deleted|none", "detail": "what changed"}],
  "risks": [{"title": "risk", "detail": "why it matters", "severity": "info|warning|critical"}],
  "handoff": {"recommended_next_action": "specific next action", "suggested_roles": ["explore|planner|implement|verify|review"], "blocking_questions": []}
}
Use empty arrays when a section has no items. Never invent command output, file changes, or evidence.
""".strip()


def _json_candidate(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1) if fenced else stripped


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


def normalize_subagent_result(text: str, *, producer: dict[str, str] | None = None) -> tuple[dict[str, Any], bool]:
    """Parse a versioned handoff and retain raw text plus diagnostics on failure."""
    raw_text = text.strip()
    try:
        payload = json.loads(_json_candidate(raw_text))
    except json.JSONDecodeError as error:
        payload = None
        errors = [f"invalid JSON: {error.msg}"]
    else:
        errors = validate_handoff(payload)
    if errors:
        return {
            "protocol_version": HANDOFF_PROTOCOL_VERSION,
            "protocol_valid": False,
            "protocol_errors": errors,
            "producer": producer or {},
            "summary": raw_text or "Worker returned no final handoff.",
            "raw_text": text,
        }, False
    assert isinstance(payload, dict)
    payload = dict(payload)
    payload["protocol_valid"] = True
    payload["protocol_errors"] = []
    payload["producer"] = producer or {}
    payload["raw_text"] = text
    return payload, True
