import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, Artifact, JobStatus
from orchestration.repository import PostgresOrchestrationRepository
from orchestration.service import create_isolated_agent_job
from orchestration.artifacts import HANDOFF_PROTOCOL_VERSION, normalize_subagent_result


@pytest.fixture
def repository() -> PostgresOrchestrationRepository:
    database_url = os.environ.get("PENHIN_DATABASE_URL")
    if not database_url:
        pytest.skip("PENHIN_DATABASE_URL is required for PostgreSQL integration tests")
    repository = PostgresOrchestrationRepository(database_url)
    repository.initialize()
    return repository


def test_postgres_job_lifecycle_records_attempt_artifact_and_events(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("inspect repository", "Inspect the repository read-only", AgentRole.EXPLORE)
    attempt = repository.start_attempt(job.id, model="test-model")
    artifact = Artifact(
        id=str(uuid4()),
        job_id=job.id,
        kind="subagent_result",
        content={"summary": "found the runtime", "findings": [], "changed_files": []},
    )

    completed = repository.finish_attempt(attempt.id, JobStatus.SUCCEEDED, artifact=artifact)

    assert completed.status == JobStatus.SUCCEEDED
    assert completed.result_artifact_id == artifact.id
    assert repository.get_artifact(artifact.id).content["summary"] == "found the runtime"
    assert [event.event_type for event in repository.list_events(job.id)] == [
        "job_created",
        "job_started",
        "job_succeeded",
    ]


def test_postgres_rejects_non_readonly_stage_one_job(repository: PostgresOrchestrationRepository) -> None:
    job_id = str(uuid4())
    with pytest.raises(ValueError, match="readonly"):
        repository.create_job(AgentJob(
            id=job_id,
            root_task_id=job_id,
            role=AgentRole.IMPLEMENT,
            subject="unsafe write",
            instruction="write files",
            workspace_mode="shared-write",
        ))


def test_postgres_rejects_invalid_terminal_transition(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("inspect", "inspect", AgentRole.EXPLORE)
    with pytest.raises(ValueError, match="Cannot start"):
        repository.start_attempt(job.id)
        repository.start_attempt(job.id)


def test_isolated_agent_job_gets_own_worktree_and_branch(repository: PostgresOrchestrationRepository) -> None:
    job = create_isolated_agent_job(repository, "inspect service boundaries", "explore")
    worktree = Path(job.worktree_path)
    try:
        assert job.workspace_mode == "readonly"
        assert worktree.is_dir()
        assert (worktree / "README.md").exists()
        assert job.worktree_branch.startswith("penhin/agent-")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=True)
        subprocess.run(["git", "branch", "-D", job.worktree_branch], check=True)


def test_handoff_protocol_accepts_complete_structured_payload() -> None:
    text = json.dumps({
        "protocol_version": HANDOFF_PROTOCOL_VERSION,
        "summary": "Repository entrypoint identified.",
        "findings": [{
            "title": "CLI entrypoint", "detail": "main.py owns the interactive loop.", "severity": "info",
            "evidence": [{"path": "main.py", "location": "main", "detail": "defines the CLI loop."}],
        }],
        "commands_run": [{"command": "pytest -q", "outcome": "passed", "detail": "197 tests passed."}],
        "changed_files": [],
        "risks": [],
        "handoff": {"recommended_next_action": "Schedule implementation.", "suggested_roles": ["implement"], "blocking_questions": []},
    })

    content, valid = normalize_subagent_result(text, producer={"job_id": "job-1", "role": "explore"})

    assert valid is True
    assert content["protocol_valid"] is True
    assert content["producer"]["job_id"] == "job-1"


def test_handoff_protocol_rejects_incomplete_payload_without_losing_raw_text() -> None:
    content, valid = normalize_subagent_result('{"summary": "not enough"}')

    assert valid is False
    assert content["protocol_valid"] is False
    assert content["raw_text"] == '{"summary": "not enough"}'
