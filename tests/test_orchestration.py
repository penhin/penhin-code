import os
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, Artifact, JobStatus
from orchestration.repository import PostgresOrchestrationRepository
from orchestration.service import run_recorded_subagent
from result import Result


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


def test_recorded_subagent_persists_normalized_handoff(repository: PostgresOrchestrationRepository, monkeypatch) -> None:
    monkeypatch.setattr("subagent.run_subagent", lambda task, agent_type: Result.success("worker found a concrete fact"))

    result = run_recorded_subagent("inspect service boundaries", agent_type="explore")

    assert result.ok is True
    job = repository.get_job(result.meta["agent_job_id"])
    assert job.status == JobStatus.SUCCEEDED
    artifact = repository.get_artifact(result.meta["artifact_id"])
    assert artifact.content["summary"] == "worker found a concrete fact"
    assert artifact.content["changed_files"] == []
