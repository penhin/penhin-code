from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CASE_SCHEMA_VERSION = "penhin.eval.case/v1"
RESULT_SCHEMA_VERSION = "penhin.eval.result/v1"
LAYERS = {"main", "subagent", "multi_agent"}
SUBAGENT_ROLES = {"explore", "plan", "general", "verification"}


@dataclass(frozen=True)
class CommandCheck:
    command: list[str]
    timeout_seconds: int = 120


@dataclass(frozen=True)
class ContentCheck:
    path: str
    contains: str


@dataclass(frozen=True)
class EvaluationCase:
    schema_version: str
    id: str
    layer: str
    category: str
    prompt: str
    fixture: str
    timeout_seconds: int
    commands: tuple[CommandCheck, ...] = ()
    content_checks: tuple[ContentCheck, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    rubric: str = ""
    agent_role: str = ""
    scenario: str = ""
    orchestration_plan: dict[str, Any] | None = None


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class JudgeScore:
    correctness: int
    relevance: int
    evidence: int
    maintainability: int
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    schema_version: str = RESULT_SCHEMA_VERSION
    run_id: str = ""
    case_id: str = ""
    repetition: int = 1
    layer: str = ""
    category: str = ""
    status: str = "pending"
    completed: bool = False
    deterministic_passed: bool = False
    safety_violations: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    final_answer: str = ""
    changed_files: list[str] = field(default_factory=list)
    diff_summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    judge: JudgeScore | None = None
    judge_error: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["judge"] = self.judge.to_dict() if self.judge else None
        return data
