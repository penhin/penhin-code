from __future__ import annotations

import json

from orchestration.repository import database_url_from_env
from orchestration.integration import apply_integration, start_integration, verify_integration
from orchestration.service import create_dag_plan, repository_from_env, wait_for_job
from result import Result


def _repository_or_failure() -> tuple[object | None, Result | None]:
    if not database_url_from_env():
        return None, Result.failure("PENHIN_DATABASE_URL is not configured", code="orchestration_unavailable")
    try:
        return repository_from_env(), None
    except Exception as error:
        return None, Result.failure(f"Orchestration storage is unavailable: {error}", code="orchestration_unavailable")


def run_agent_job_show(id: str) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    job = repository.get_job(id)
    if job is None:
        return Result.failure(f"Agent job {id} not found", code="not_found")
    return Result.success(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), data=job.to_dict())


def run_agent_job_list(root_task_id: str = "", status: str = "") -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    from orchestration.models import JobStatus
    try:
        jobs = repository.list_jobs(root_task_id or None, JobStatus(status) if status else None)
    except ValueError:
        return Result.failure(f"Unknown job status: {status}", code="invalid_status")
    data = [job.to_dict() for job in jobs]
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def run_agent_artifact_show(job_id: str) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    job = repository.get_job(job_id)
    if job is None:
        return Result.failure(f"Agent job {job_id} not found", code="not_found")
    if not job.result_artifact_id:
        return Result.failure(f"Agent job {job_id} has no result artifact", code="artifact_not_found")
    artifact = repository.get_artifact(job.result_artifact_id)
    if artifact is None:
        return Result.failure(f"Result artifact {job.result_artifact_id} not found", code="artifact_not_found")
    data = {"id": artifact.id, "job_id": artifact.job_id, "kind": artifact.kind, "content": artifact.content, "schema_valid": artifact.schema_valid, "created_at": artifact.created_at}
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def run_agent_job_cancel(id: str) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    try:
        job = repository.request_cancel(id)
    except KeyError:
        return Result.failure(f"Agent job {id} not found", code="not_found")
    return Result.success(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), data=job.to_dict())


def run_agent_plan_create(goal: str) -> Result:
    if not goal.strip():
        return Result.failure("goal must not be empty", code="invalid_goal")
    return create_dag_plan(goal)


def run_agent_dag_show(root_task_id: str) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    jobs = repository.list_jobs(root_task_id)
    if not jobs:
        return Result.failure(f"No DAG jobs found for root task {root_task_id}", code="not_found")
    by_id = {job.id: job for job in jobs}
    nodes = []
    for job in jobs:
        blockers = [dependency_id for dependency_id in job.depends_on if by_id.get(dependency_id) and by_id[dependency_id].status != "succeeded"]
        node = job.to_dict()
        node["blocked_by"] = blockers
        node["ready"] = job.status == "queued" and not blockers
        nodes.append(node)
    data = {"root_task_id": root_task_id, "jobs": nodes}
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def run_agent_job_wait(id: str, timeout_seconds: int = 30) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    if timeout_seconds < 1 or timeout_seconds > 900:
        return Result.failure("timeout_seconds must be between 1 and 900", code="invalid_timeout")
    outcome = wait_for_job(repository, id, timeout_seconds)
    if not outcome.ok:
        return outcome
    job = outcome.data["job"]
    artifact = outcome.data["artifact"]
    data = {
        "job": job,
        "artifact": {
            "id": artifact.id,
            "kind": artifact.kind,
            "content": artifact.content,
            "schema_valid": artifact.schema_valid,
        },
    }
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def _integration_data(repository, run_id: str) -> dict:
    run = repository.get_integration_run(run_id)
    if run is None:
        raise KeyError(run_id)
    return {"run": run.to_dict(), "items": [item.to_dict() for item in repository.list_integration_items(run_id)]}


def run_integration_start(root_task_id: str, job_ids: list[str]) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    try:
        run = start_integration(repository, root_task_id, job_ids)
        run = apply_integration(repository, run.id)
        data = _integration_data(repository, run.id)
    except (KeyError, ValueError, RuntimeError) as error:
        return Result.failure(str(error), code="integration_start_failed")
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def run_integration_show(id: str) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    try:
        data = _integration_data(repository, id)
    except KeyError:
        return Result.failure(f"Integration run {id} not found", code="not_found")
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)


def run_integration_verify(id: str, command: list[str]) -> Result:
    repository, failure = _repository_or_failure()
    if failure:
        return failure
    try:
        run = verify_integration(repository, id, command)
        data = _integration_data(repository, run.id)
    except (KeyError, ValueError, RuntimeError) as error:
        return Result.failure(str(error), code="integration_verification_failed")
    if run.status != "verified":
        return Result.failure(run.error or "Integration verification failed", code="verification_failed", data=data)
    return Result.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)
