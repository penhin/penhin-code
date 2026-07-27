from __future__ import annotations

import logging
from uuid import uuid4

from result import Result

from .artifacts import normalize_subagent_result
from .models import AgentJob, AgentRole, Artifact, JobStatus
from .repository import PostgresOrchestrationRepository, database_url_from_env
from .scheduler import PersistentScheduler


logger = logging.getLogger("penhin.orchestration")


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


def agent_type_for_role(role: AgentRole) -> str:
    return {
        AgentRole.PLANNER: "plan",
        AgentRole.EXPLORE: "explore",
        AgentRole.VERIFY: "verification",
    }.get(role, "general")


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
    role = ROLE_BY_AGENT_TYPE.get(agent_type, AgentRole.GENERAL)
    if root_task_id:
        job = scheduler.repository.create_job(AgentJob(
            id=str(uuid4()), root_task_id=root_task_id, parent_id=root_task_id,
            role=role, subject=task[:160], instruction=task,
        ))
    else:
        job = scheduler.repository.create_root_job(task[:160], task, role)
    if dispatch:
        scheduler.dispatch()
    return job


def run_recorded_subagent(task: str, agent_type: str = "general", root_task_id: str | None = None) -> Result:
    """Run the legacy worker while atomically recording its first-stage artifacts."""
    from subagent import run_subagent

    try:
        repository = repository_from_env()
    except Exception as error:
        logger.warning("[orchestration] database unavailable; running unrecorded subagent: %s", error)
        return run_subagent(task, agent_type=agent_type)
    if repository is None:
        return run_subagent(task, agent_type=agent_type)

    role = ROLE_BY_AGENT_TYPE.get(agent_type, AgentRole.GENERAL)
    if root_task_id:
        job = repository.create_job(AgentJob(
            id=str(uuid4()),
            root_task_id=root_task_id,
            parent_id=root_task_id,
            role=role,
            subject=task[:160],
            instruction=task,
        ))
    else:
        job = repository.create_root_job(subject=task[:160], instruction=task, role=role)
    attempt = repository.start_attempt(job.id)
    result = run_subagent(task, agent_type=agent_type)
    if result.ok:
        content, schema_valid = normalize_subagent_result(result.message)
        artifact = Artifact(id=str(uuid4()), job_id=job.id, kind="subagent_result", content=content, schema_valid=schema_valid)
        finished = repository.finish_attempt(attempt.id, JobStatus.SUCCEEDED, artifact=artifact, terminal_reason="completed")
        result.meta["agent_job_id"] = finished.id
        result.meta["artifact_id"] = artifact.id
        return result
    finished = repository.finish_attempt(attempt.id, JobStatus.FAILED, error=result.error, terminal_reason=result.meta.get("code", "failed"))
    result.meta["agent_job_id"] = finished.id
    return result
