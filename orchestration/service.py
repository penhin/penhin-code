from __future__ import annotations

import atexit
import json
import os
import time
from uuid import uuid4

from result import Result

from .models import AgentJob, AgentRole, JobStatus, TERMINAL_JOB_STATUSES
from .planning import DAG_PROTOCOL_VERSION
from .repositories import OrchestrationRepository, database_url_from_env, repository_from_database_url
from .scheduler import PersistentScheduler
from .settings import agent_poll_interval_seconds, sync_agent_timeout_seconds
from .worktrees import provision_worktree


def repository_from_env() -> OrchestrationRepository:
    database_url = database_url_from_env()
    repository = repository_from_database_url(database_url)
    repository.initialize()
    return repository


ROLE_BY_AGENT_TYPE = {
    "plan": AgentRole.PLANNER,
    "explore": AgentRole.EXPLORE,
    "verification": AgentRole.VERIFY,
    "general": AgentRole.GENERAL,
}


_scheduler: PersistentScheduler | None = None


def _shutdown_scheduler_at_exit() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


def scheduler_from_env() -> PersistentScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    repository = repository_from_env()
    _scheduler = PersistentScheduler(repository)
    _scheduler.start()
    atexit.register(_shutdown_scheduler_at_exit)
    return _scheduler


def enqueue_subagent_job(
    task: str,
    agent_type: str = "general",
    root_task_id: str | None = None,
    dispatch: bool = True,
) -> AgentJob:
    scheduler = scheduler_from_env()
    job = create_isolated_agent_job(scheduler.repository, task, agent_type, root_task_id)
    if dispatch:
        scheduler.dispatch()
    return job


def workspace_mode_for_agent(agent_type: str) -> str:
    return "isolated_write" if agent_type == "general" else "readonly"


def create_isolated_agent_job(
    repository: OrchestrationRepository,
    task: str,
    agent_type: str,
    root_task_id: str | None = None,
    depends_on: list[str] | None = None,
    priority: int = 0,
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
        depends_on=depends_on or [],
        priority=priority,
        workspace_mode=workspace_mode_for_agent(agent_type),
        worktree_path=worktree.path,
        worktree_branch=worktree.branch,
    ))


def wait_for_job(repository: OrchestrationRepository, job_id: str, timeout_seconds: int) -> Result:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = repository.get_job(job_id)
        if job is None:
            return Result.failure("Agent job disappeared", code="agent_job_missing")
        if job.status == JobStatus.SUCCEEDED:
            artifact = repository.get_artifact(job.result_artifact_id) if job.result_artifact_id else None
            if artifact is None:
                return Result.failure("Agent completed without a result artifact", code="missing_artifact")
            return Result.success(
                json.dumps(artifact.content, ensure_ascii=False),
                data={"job": job.to_dict(), "artifact": artifact},
            )
        if job.status in TERMINAL_JOB_STATUSES:
            return Result.failure(job.error or f"Agent finished with status {job.status}", code=str(job.status), job=job.to_dict())
        time.sleep(agent_poll_interval_seconds())
    return Result.failure("Timed out waiting for agent job", code="agent_wait_timeout", agent_job_id=job_id)


def materialize_dag_plan(repository: OrchestrationRepository, planner_job_id: str, plan: dict) -> dict:
    """Create isolated persistent jobs for a validated penhin.dag/v1 plan."""
    if plan.get("protocol_version") != DAG_PROTOCOL_VERSION:
        raise ValueError("DAG protocol version is not supported")
    jobs = plan["jobs"]
    created: dict[str, AgentJob] = {}
    remaining = {job["key"]: job for job in jobs}
    while remaining:
        ready = [job for job in remaining.values() if all(key in created for key in job["depends_on"])]
        if not ready:
            raise ValueError("DAG cannot be materialized because dependencies are cyclic")
        for spec in ready:
            created[spec["key"]] = create_isolated_agent_job(
                repository,
                spec["instruction"],
                spec["agent_type"],
                root_task_id=planner_job_id,
                depends_on=[created[key].id for key in spec["depends_on"]],
                priority=spec.get("priority", 0),
            )
            del remaining[spec["key"]]
    return {
        "root_task_id": planner_job_id,
        "goal": plan["goal"],
        "job_ids": {key: job.id for key, job in created.items()},
        "final_job_keys": plan["final_job_keys"],
        "final_job_ids": [created[key].id for key in plan["final_job_keys"]],
    }


def create_dag_plan(goal: str) -> Result:
    """Run the planner, validate its structured artifact, then enqueue the DAG."""
    try:
        planner = enqueue_subagent_job(goal, agent_type="plan", dispatch=True)
    except Exception as error:
        return Result.failure(f"Unable to start Planner: {error}", code="planner_start_failed")
    repository = repository_from_env()
    timeout_seconds = sync_agent_timeout_seconds()
    outcome = wait_for_job(repository, planner.id, timeout_seconds)
    if not outcome.ok:
        return outcome
    artifact = outcome.data["artifact"]
    content = artifact.content
    if artifact.kind != "agent_dag_plan.v1" or not artifact.schema_valid or not content.get("protocol_valid"):
        return Result.failure(
            "Planner returned an invalid DAG protocol artifact",
            code="invalid_dag_plan",
            planner_job_id=planner.id,
            protocol_errors=content.get("protocol_errors", []),
            raw_text=content.get("raw_text", ""),
        )
    try:
        data = materialize_dag_plan(repository, planner.id, content)
        scheduler = scheduler_from_env()
        if scheduler:
            scheduler.dispatch()
    except Exception as error:
        return Result.failure(f"Unable to materialize DAG: {error}", code="dag_materialization_failed", planner_job_id=planner.id)
    data.update({"planner_job_id": planner.id, "planner_artifact_id": artifact.id})
    return Result.success(json.dumps(data, ensure_ascii=False), data=data)


def run_recorded_subagent(task: str, agent_type: str = "general", root_task_id: str | None = None) -> Result:
    """Run a delegated agent in its own worktree and wait for its durable handoff."""
    try:
        job = enqueue_subagent_job(task, agent_type=agent_type, root_task_id=root_task_id)
    except Exception as error:
        return Result.failure(f"Unable to start isolated agent: {error}", code="agent_start_failed")
    timeout_seconds = sync_agent_timeout_seconds()
    repository = repository_from_env()
    outcome = wait_for_job(repository, job.id, timeout_seconds)
    if not outcome.ok:
        if outcome.meta.get("code") == "agent_wait_timeout":
            repository.request_cancel(job.id)
        return Result.failure(outcome.error, code=outcome.meta.get("code", "agent_failed"), agent_job_id=job.id)
    current = outcome.data["job"]
    artifact = outcome.data["artifact"]
    return Result.success(
        json.dumps(artifact.content, ensure_ascii=False),
        data=artifact.content,
        agent_job_id=current["id"],
        artifact_id=artifact.id,
        worktree_path=current["worktree_path"],
        worktree_branch=current["worktree_branch"],
    )
