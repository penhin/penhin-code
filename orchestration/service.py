from __future__ import annotations

import json
import os
import time
from uuid import uuid4

from result import Result

from .models import AgentJob, AgentRole, JobStatus
from .repository import PostgresOrchestrationRepository, database_url_from_env
from .scheduler import PersistentScheduler
from .worktrees import provision_worktree


def repository_from_env() -> PostgresOrchestrationRepository | None:
    database_url = database_url_from_env()
    if not database_url:
        return None
    repository = PostgresOrchestrationRepository(database_url)
    repository.initialize()
    return repository


ROLE_BY_AGENT_TYPE = {
    "plan": AgentRole.PLANNER,
    "explore": AgentRole.EXPLORE,
    "verification": AgentRole.VERIFY,
    "general": AgentRole.GENERAL,
}


_scheduler: PersistentScheduler | None = None


def scheduler_from_env() -> PersistentScheduler | None:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    repository = repository_from_env()
    if repository is None:
        return None
    _scheduler = PersistentScheduler(repository)
    _scheduler.start()
    return _scheduler


def enqueue_subagent_job(
    task: str,
    agent_type: str = "general",
    root_task_id: str | None = None,
    dispatch: bool = True,
) -> AgentJob:
    scheduler = scheduler_from_env()
    if scheduler is None:
        raise RuntimeError("PENHIN_DATABASE_URL is not configured")
    job = create_isolated_agent_job(scheduler.repository, task, agent_type, root_task_id)
    if dispatch:
        scheduler.dispatch()
    return job


def workspace_mode_for_agent(agent_type: str) -> str:
    return "isolated_write" if agent_type == "general" else "readonly"


def create_isolated_agent_job(
    repository: PostgresOrchestrationRepository,
    task: str,
    agent_type: str,
    root_task_id: str | None = None,
) -> AgentJob:
    job_id = str(uuid4())
    worktree = provision_worktree(job_id)
    role = ROLE_BY_AGENT_TYPE.get(agent_type, AgentRole.GENERAL)
    return repository.create_job(AgentJob(
        id=job_id,
        root_task_id=root_task_id or job_id,
        parent_id=root_task_id,
        role=role,
        subject=task[:160],
        instruction=task,
        workspace_mode=workspace_mode_for_agent(agent_type),
        worktree_path=worktree.path,
        worktree_branch=worktree.branch,
    ))


def run_recorded_subagent(task: str, agent_type: str = "general", root_task_id: str | None = None) -> Result:
    """Run a delegated agent in its own worktree and wait for its durable handoff."""
    try:
        job = enqueue_subagent_job(task, agent_type=agent_type, root_task_id=root_task_id)
    except Exception as error:
        return Result.failure(f"Unable to start isolated agent: {error}", code="agent_start_failed")
    timeout_seconds = int(os.getenv("PENHIN_SYNC_AGENT_TIMEOUT_SECONDS", "900"))
    deadline = time.monotonic() + timeout_seconds
    repository = repository_from_env()
    assert repository is not None
    while time.monotonic() < deadline:
        current = repository.get_job(job.id)
        if current is None:
            return Result.failure("Agent job disappeared", code="agent_job_missing")
        if current.status == JobStatus.SUCCEEDED:
            artifact = repository.get_artifact(current.result_artifact_id) if current.result_artifact_id else None
            if artifact is None:
                return Result.failure("Agent completed without a handoff artifact", code="missing_artifact")
            return Result.success(
                json.dumps(artifact.content, ensure_ascii=False),
                data=artifact.content,
                agent_job_id=current.id,
                artifact_id=artifact.id,
                worktree_path=current.worktree_path,
                worktree_branch=current.worktree_branch,
            )
        if current.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT, JobStatus.INTERRUPTED}:
            return Result.failure(current.error or f"Agent finished with status {current.status}", code=str(current.status), agent_job_id=current.id)
        time.sleep(0.1)
    repository.request_cancel(job.id)
    return Result.failure("Timed out waiting for isolated agent", code="agent_wait_timeout", agent_job_id=job.id)
