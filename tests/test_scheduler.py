import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, JobStatus
from orchestration.repository import PostgresOrchestrationRepository
from orchestration.scheduler import PersistentScheduler


class SchedulerForTest(PersistentScheduler):
    def __init__(self, repository, *, sleep_seconds: float = 0.01, max_workers: int = 1):
        super().__init__(repository, max_workers=max_workers)
        self.sleep_seconds = sleep_seconds

    def _spawn_worker(self, _job, _attempt):
        return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({self.sleep_seconds})"], start_new_session=True)


@pytest.fixture
def repository() -> PostgresOrchestrationRepository:
    database_url = os.environ.get("PENHIN_DATABASE_URL")
    if not database_url:
        pytest.skip("PENHIN_DATABASE_URL is required for PostgreSQL scheduler integration tests")
    repository = PostgresOrchestrationRepository(database_url)
    repository.initialize()
    return repository


def wait_for(repository: PostgresOrchestrationRepository, job_id: str, status: JobStatus) -> None:
    for _ in range(100):
        if repository.get_job(job_id).status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}")


def test_scheduler_marks_worker_exit_as_failure(repository: PostgresOrchestrationRepository) -> None:
    scheduler = SchedulerForTest(repository)
    scheduler.start()
    job = repository.create_root_job("queued", "queued", AgentRole.EXPLORE)
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
    job_id = str(uuid4())
    job = repository.create_job(AgentJob(
        id=job_id, root_task_id=job_id, role=AgentRole.EXPLORE, subject="retry", instruction="retry", max_attempts=2,
    ))
    scheduler = SchedulerForTest(repository)
    scheduler.start()
    wait_for(repository, job.id, JobStatus.FAILED)
    assert repository.get_job(job.id).attempt_count == 2
    scheduler.shutdown(wait=True)


def test_scheduler_cancels_queued_job(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("cancel", "cancel", AgentRole.EXPLORE)
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
    job_id = str(uuid4())
    job = repository.create_job(AgentJob(
        id=job_id, root_task_id=job_id, role=AgentRole.EXPLORE, subject="timeout", instruction="timeout", timeout_seconds=1,
    ))
    scheduler = SchedulerForTest(repository, sleep_seconds=10)
    scheduler.start()
    wait_for(repository, job.id, JobStatus.TIMED_OUT)
    scheduler.shutdown(wait=True)
