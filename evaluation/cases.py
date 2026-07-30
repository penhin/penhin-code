from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .models import CASE_SCHEMA_VERSION, LAYERS, SUBAGENT_ROLES, CommandCheck, ContentCheck, EvaluationCase


CASE_KEYS = {
    "schema_version", "id", "layer", "category", "prompt", "fixture", "timeout_seconds",
    "commands", "content_checks", "allowed_paths", "forbidden_paths", "expected_tools",
    "rubric", "agent_role", "scenario", "orchestration_plan",
}
SUITE_SCHEMA_VERSION = "penhin.eval.suite/v1"


def safe_relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError(f"{field} must be a safe relative path: {value!r}")
    return path.as_posix()


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(value)


def parse_case(data: Any, suite_dir: Path) -> EvaluationCase:
    if not isinstance(data, dict):
        raise ValueError("case must be an object")
    unknown = sorted(set(data) - CASE_KEYS)
    if unknown:
        raise ValueError(f"unknown case fields: {', '.join(unknown)}")
    required = {"schema_version", "id", "layer", "category", "prompt", "fixture", "timeout_seconds"}
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"missing case fields: {', '.join(missing)}")
    if data["schema_version"] != CASE_SCHEMA_VERSION:
        raise ValueError(f"schema_version must equal {CASE_SCHEMA_VERSION}")
    if not isinstance(data["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", data["id"]):
        raise ValueError("case id must be a lowercase slug")
    if data["layer"] not in LAYERS:
        raise ValueError(f"layer must be one of {sorted(LAYERS)}")
    for field in ("category", "prompt"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    timeout = data["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    fixture = safe_relative_path(str(data["fixture"]), "fixture")
    fixture_path = (suite_dir / fixture).resolve()
    if not fixture_path.is_relative_to(suite_dir.resolve()) or not fixture_path.is_dir():
        raise ValueError(f"fixture directory does not exist: {fixture}")
    commands: list[CommandCheck] = []
    for index, item in enumerate(data.get("commands", [])):
        if not isinstance(item, dict) or set(item) - {"command", "timeout_seconds"}:
            raise ValueError(f"commands[{index}] has invalid fields")
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f"commands[{index}].command must be a non-empty string array")
        commands.append(CommandCheck(command, int(item.get("timeout_seconds", 120))))
    content_checks: list[ContentCheck] = []
    for index, item in enumerate(data.get("content_checks", [])):
        if not isinstance(item, dict) or set(item) != {"path", "contains"}:
            raise ValueError(f"content_checks[{index}] must contain only path and contains")
        content_checks.append(ContentCheck(safe_relative_path(str(item["path"]), f"content_checks[{index}].path"), str(item["contains"])))
    allowed = tuple(safe_relative_path(item, "allowed_paths") for item in _string_list(data.get("allowed_paths"), "allowed_paths"))
    forbidden = tuple(safe_relative_path(item, "forbidden_paths") for item in _string_list(data.get("forbidden_paths"), "forbidden_paths"))
    role = str(data.get("agent_role", ""))
    if data["layer"] == "subagent" and role not in SUBAGENT_ROLES:
        raise ValueError(f"subagent case requires agent_role in {sorted(SUBAGENT_ROLES)}")
    orchestration_plan = data.get("orchestration_plan")
    if orchestration_plan is not None:
        if data["layer"] != "multi_agent":
            raise ValueError("orchestration_plan is only valid for multi_agent cases")
        from orchestration.planning import validate_dag_plan
        plan_errors = validate_dag_plan(orchestration_plan)
        if plan_errors:
            raise ValueError(f"invalid orchestration_plan: {'; '.join(plan_errors)}")
    return EvaluationCase(
        schema_version=data["schema_version"], id=data["id"], layer=data["layer"], category=data["category"],
        prompt=data["prompt"], fixture=fixture, timeout_seconds=timeout, commands=tuple(commands),
        content_checks=tuple(content_checks), allowed_paths=allowed, forbidden_paths=forbidden,
        expected_tools=_string_list(data.get("expected_tools"), "expected_tools"),
        rubric=str(data.get("rubric", "")), agent_role=role, scenario=str(data.get("scenario", "")),
        orchestration_plan=orchestration_plan,
    )


def load_suite(suite: str, root: Path | None = None) -> list[EvaluationCase]:
    suites_root = (root or Path(__file__).parent / "suites").resolve()
    safe_relative_path(suite, "suite")
    suite_path = suites_root / suite / "suite.yaml"
    if not suite_path.is_file():
        raise ValueError(f"suite not found: {suite}")
    data = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "cases"}:
        raise ValueError("suite must contain only schema_version and cases")
    if data["schema_version"] != SUITE_SCHEMA_VERSION or not isinstance(data["cases"], list):
        raise ValueError(f"suite schema_version must equal {SUITE_SCHEMA_VERSION} and cases must be an array")
    cases = [parse_case(item, suite_path.parent) for item in data["cases"]]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("suite contains duplicate case ids")
    return cases
