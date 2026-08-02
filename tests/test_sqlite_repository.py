from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from penhin.orchestration.models import AgentJob, AgentRole, Artifact, IntegrationItem, IntegrationItemStatus, IntegrationRun, IntegrationRunStatus, JobStatus
from penhin.orchestration.scheduler import PersistentScheduler
from penhin.orchestration.repositories import database_url_from_env, repository_from_database_url
from penhin.orchestration.repositories.sqlite_repository import SqliteOrchestrationRepository, sqlite_database_url


@pytest.fixture
def repository(tmp_path: Path) -> SqliteOrchestrationRepository:
    store = SqliteOrchestrationRepository(sqlite_database_url(tmp_path / "orchestration.sqlite3"))
    store.initialize()
    return store


def executable_job(repository: SqliteOrchestrationRepository, subject: str, **kwargs) -> AgentJob:
    job_id = str(uuid4())
    return repository.create_job(AgentJob(
        id=job_id, root_task_id=job_id, role=AgentRole.EXPLORE, subject=subject, instruction=subject,
        worktree_path=str(Path.cwd()), worktree_branch="test-worktree", **kwargs,
    ))


def test_sqlite_records_job_lifecycle_artifact_and_events(repository: SqliteOrchestrationRepository) -> None:
    job = repository.create_root_job("inspect", "Inspect", AgentRole.EXPLORE)
    attempt = repository.start_attempt(job.id, model="test")
    artifact = Artifact(id=str(uuid4()), job_id=job.id, kind="test", content={"summary": "done"})

    completed = repository.finish_attempt(attempt.id, JobStatus.SUCCEEDED, artifact=artifact)

    assert completed.status == JobStatus.SUCCEEDED
    assert repository.get_artifact(artifact.id).content == {"summary": "done"}
    assert [event.event_type for event in repository.list_events(job.id)] == ["job_created", "job_started", "job_succeeded"]


def test_agent_job_instruction_is_redacted_before_persistence(
    repository: SqliteOrchestrationRepository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from penhin.auth.secrets import register_secret
    from penhin.orchestration import service

    register_secret("job-secret-sentinel")
    monkeypatch.setattr(service, "provision_worktree", lambda _job_id: SimpleNamespace(
        path=str(tmp_path), branch="penhin/test-redaction",
    ))

    job = service.create_isolated_agent_job(repository, "inspect job-secret-sentinel", "explore")

    assert job.instruction == "inspect <redacted>"
    assert job.subject == "inspect <redacted>"


def test_sqlite_claim_respects_dependencies_and_records_integration(repository: SqliteOrchestrationRepository) -> None:
    parent = executable_job(repository, "parent")
    child = repository.create_job(AgentJob(
        id=str(uuid4()), root_task_id=parent.id, parent_id=parent.id, role=AgentRole.GENERAL,
        subject="child", instruction="child", depends_on=[parent.id], workspace_mode="isolated_write",
        worktree_path="/tmp/child", worktree_branch="penhin/child",
    ))
    claim = repository.claim_next_job()
    assert claim is not None and claim[0].id == parent.id
    repository.finish_attempt(claim[1].id, JobStatus.SUCCEEDED, artifact=Artifact(id=str(uuid4()), job_id=parent.id, kind="test", content={}))
    assert repository.claim_next_job()[0].id == child.id

    root = repository.create_root_job("integration", "integration", status=JobStatus.SUCCEEDED)
    source = repository.create_job(AgentJob(id=str(uuid4()), root_task_id=root.id, parent_id=root.id, role=AgentRole.GENERAL, subject="source", instruction="source", workspace_mode="isolated_write", worktree_path="/tmp/source", worktree_branch="penhin/source"))
    run = IntegrationRun(id=str(uuid4()), root_task_id=root.id, base_commit="a" * 40, worktree_path="/tmp/integration", worktree_branch="penhin/integration")
    item = IntegrationItem(id=str(uuid4()), run_id=run.id, job_id=source.id, ordinal=0, source_branch=source.worktree_branch, commits=["b" * 40])
    repository.create_integration_run(run, [item])
    repository.transition_integration_item(item.id, IntegrationItemStatus.APPLYING)
    repository.transition_integration_item(item.id, IntegrationItemStatus.APPLIED)
    repository.transition_integration_run(run.id, IntegrationRunStatus.APPLYING)
    repository.transition_integration_run(run.id, IntegrationRunStatus.INTEGRATED, result_commit="c" * 40)
    assert repository.get_integration_run(run.id).result_commit == "c" * 40


class SchedulerForTest(PersistentScheduler):
    def _spawn_worker(self, _job, _attempt):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.01)"], start_new_session=True)


def test_sqlite_scheduler_retries_and_recovers(repository: SqliteOrchestrationRepository) -> None:
    interrupted = repository.create_root_job("interrupted", "interrupted", AgentRole.EXPLORE)
    repository.start_attempt(interrupted.id)
    job = executable_job(repository, "retry", max_attempts=2)
    scheduler = SchedulerForTest(repository, max_workers=1)
    scheduler.start()
    for _ in range(100):
        current = repository.get_job(job.id)
        if current.status == JobStatus.FAILED and current.attempt_count == 2:
            break
        time.sleep(0.02)
    scheduler.shutdown(wait=True)
    assert repository.get_job(interrupted.id).status == JobStatus.INTERRUPTED
    assert repository.get_job(job.id).attempt_count == 2


def test_repository_factory_defaults_to_project_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PENHIN_DATABASE_URL", raising=False)
    url = database_url_from_env()
    repository = repository_from_database_url(url)
    repository.initialize()
    assert isinstance(repository, SqliteOrchestrationRepository)
    assert repository.path == tmp_path / ".penhin" / "orchestration.sqlite3"
    assert repository.database_url == url


def test_repository_factory_accepts_postgres_and_rejects_unknown_url() -> None:
    assert repository_from_database_url("postgresql://user:pass@localhost/db").backend_name == "postgresql"
    with pytest.raises(ValueError, match="must use"):
        repository_from_database_url("mysql://localhost/db")
