from __future__ import annotations

import json

from orchestration.repository import database_url_from_env
from orchestration.service import repository_from_env
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
