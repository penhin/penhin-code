from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class AgentRole(StrEnum):
    PLANNER = "planner"
    EXPLORE = "explore"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW = "review"
    GENERAL = "general"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.TIMED_OUT,
}


@dataclass
class AgentJob:
    id: str
    role: AgentRole
    subject: str
    instruction: str
    root_task_id: str
    parent_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    priority: int = 0
    depends_on: list[str] = field(default_factory=list)
    workspace_mode: str = "readonly"
    worktree_path: str = ""
    worktree_branch: str = ""
    max_turns: int | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    max_attempts: int = 1
    attempt_count: int = 0
    cancel_requested: bool = False
    worker_pid: int | None = None
    worker_token: str = ""
    result_artifact_id: str | None = None
    error: str = ""
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobAttempt:
    id: str
    job_id: str
    number: int
    status: JobStatus = JobStatus.RUNNING
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    terminal_reason: str = ""
    error: str = ""
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class Artifact:
    id: str
    job_id: str
    kind: str
    content: dict[str, Any]
    schema_valid: bool = True
    created_at: str | None = None


@dataclass
class JobEvent:
    id: str
    job_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


@dataclass
class IntegrationRun:
    id: str
    root_task_id: str
    base_commit: str
    worktree_path: str
    worktree_branch: str
    status: str = "created"
    result_commit: str = ""
    error: str = ""
    created_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IntegrationItem:
    id: str
    run_id: str
    job_id: str
    ordinal: int
    source_branch: str
    commits: list[str]
    status: str = "pending"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
