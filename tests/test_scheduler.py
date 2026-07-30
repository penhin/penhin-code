import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, JobStatus
from orchestration.repositories.postgres_repository import PostgresOrchestrationRepository
from orchestration.scheduler import PersistentScheduler


class SchedulerForTest(PersistentScheduler):
    def __init__(self, repository, *, sleep_seconds: float = 0.01, max_workers: int = 1):
        super().__init__(repository, max_workers=max_workers)
        self.sleep_seconds = sleep_seconds

    def _spawn_worker(self, _job, _attempt):
        return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({self.sleep_seconds})"], start_new_session=True)


@pytest.fixture
def repository() -> PostgresOrchestrationRepository:
    database_url = os.environ.get("PENHIN_TEST_POSTGRES_URL", "")
    if not database_url:
        pytest.skip("PENHIN_TEST_POSTGRES_URL is required for PostgreSQL scheduler integration tests")
    repository = PostgresOrchestrationRepository(database_url)
    repository.initialize()
    return repository


def wait_for(repository: PostgresOrchestrationRepository, job_id: str, status: JobStatus) -> None:
    for _ in range(100):
        if repository.get_job(job_id).status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}")


def executable_job(repository: PostgresOrchestrationRepository, subject: str, **kwargs):
    job_id = str(uuid4())
    return repository.create_job(AgentJob(
        id=job_id,
        root_task_id=job_id,
        role=AgentRole.EXPLORE,
        subject=subject,
        instruction=subject,
        worktree_path=str(Path.cwd()),
        worktree_branch="test-worktree",
        **kwargs,
    ))


def test_scheduler_marks_worker_exit_as_failure(repository: PostgresOrchestrationRepository) -> None:
    scheduler = SchedulerForTest(repository)
    scheduler.start()
    job = executable_job(repository, "queued")
    scheduler.dispatch()
    wait_for(repository, job.id, JobStatus.FAILED)
    scheduler.shutdown(wait=True)


def test_scheduler_recovers_running_job_without_duplicate_claim(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("interrupted", "interrupted", AgentRole.EXPLORE)
    repository.start_attempt(job.id)
    scheduler = SchedulerForTest(repository)
    scheduler.start()
    assert repository.get_job(job.id).status == JobStatus.INTERRUPTED
    scheduler.shutdown(wait=True)


def test_scheduler_retries_failed_job_up_to_attempt_budget(repository: PostgresOrchestrationRepository) -> None:
    job = executable_job(repository, "retry", max_attempts=2)
    scheduler = SchedulerForTest(repository)
    scheduler.start()
    wait_for(repository, job.id, JobStatus.FAILED)
    assert repository.get_job(job.id).attempt_count == 2
    scheduler.shutdown(wait=True)


def test_scheduler_cancels_queued_job(repository: PostgresOrchestrationRepository) -> None:
    job = executable_job(repository, "cancel")
    scheduler = SchedulerForTest(repository, sleep_seconds=10)
    scheduler.start()
    scheduler.dispatch()
    for _ in range(100):
        if repository.get_job(job.id).status == JobStatus.RUNNING:
            break
        time.sleep(0.02)
    cancelled = scheduler.request_cancel(job.id)
    assert cancelled.cancel_requested is True
    wait_for(repository, job.id, JobStatus.CANCELLED)
    scheduler.shutdown(wait=True)


def test_scheduler_timeout_terminates_worker_process(repository: PostgresOrchestrationRepository) -> None:
    job = executable_job(repository, "timeout", timeout_seconds=1)
    scheduler = SchedulerForTest(repository, sleep_seconds=10)
    scheduler.start()
    wait_for(repository, job.id, JobStatus.TIMED_OUT)
    scheduler.shutdown(wait=True)


def test_scheduler_shutdown_stops_callback_dispatch(repository: PostgresOrchestrationRepository) -> None:
    scheduler = SchedulerForTest(repository, sleep_seconds=0.05)
    scheduler.start()
    job = executable_job(repository, "shutdown")
    scheduler.dispatch()
    scheduler.shutdown(wait=False)
    time.sleep(0.1)
    assert scheduler._started is False
    assert repository.get_job(job.id).status in {JobStatus.RUNNING, JobStatus.FAILED}
