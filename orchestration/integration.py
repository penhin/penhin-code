from __future__ import annotations

import subprocess
from uuid import uuid4

from .models import IntegrationItem, IntegrationRun, JobStatus
from .repository import PostgresOrchestrationRepository
from .worktrees import provision_integration_worktree


def _git(worktree: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _change_set_for_job(repository: PostgresOrchestrationRepository, job_id: str) -> tuple[object, dict]:
    job = repository.get_job(job_id)
    if job is None:
        raise ValueError(f"Job {job_id} does not exist")
    if job.status != JobStatus.SUCCEEDED:
        raise ValueError(f"Job {job_id} is not succeeded")
    artifact = repository.get_artifact(job.result_artifact_id) if job.result_artifact_id else None
    change_set = artifact.content.get("change_set") if artifact and artifact.schema_valid else None
    if not isinstance(change_set, dict) or not isinstance(change_set.get("base_commit"), str) or not isinstance(change_set.get("commits"), list):
        raise ValueError(f"Job {job_id} has no valid committed change_set")
    if not all(isinstance(commit, str) and commit for commit in change_set["commits"]):
        raise ValueError(f"Job {job_id} has an invalid change_set commit list")
    return job, change_set


def start_integration(repository: PostgresOrchestrationRepository, root_task_id: str, job_ids: list[str]) -> IntegrationRun:
    if not job_ids:
        raise ValueError("job_ids must not be empty")
    selected = [_change_set_for_job(repository, job_id) for job_id in job_ids]
    bases = {change_set["base_commit"] for _, change_set in selected}
    if len(bases) != 1:
        raise ValueError("All change sets must share the same base_commit")
    base_commit = bases.pop()
    run_id = str(uuid4())
    worktree = provision_integration_worktree(run_id, base_commit)
    items = [
        IntegrationItem(
            id=str(uuid4()), run_id=run_id, job_id=job.id, ordinal=index,
            source_branch=job.worktree_branch, commits=change_set["commits"],
        )
        for index, (job, change_set) in enumerate(selected)
    ]
    return repository.create_integration_run(IntegrationRun(
        id=run_id, root_task_id=root_task_id, base_commit=base_commit,
        worktree_path=worktree.path, worktree_branch=worktree.branch,
    ), items)


def apply_integration(repository: PostgresOrchestrationRepository, run_id: str) -> IntegrationRun:
    run = repository.get_integration_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run.status not in {"created", "needs_resolution"}:
        raise ValueError(f"Integration run {run_id} is already {run.status}")
    for item in repository.list_integration_items(run_id):
        if item.status == "applied":
            continue
        if item.status == "conflict":
            raise ValueError("Resolve the recorded conflict in the integration worktree before retrying")
        try:
            for commit in item.commits:
                _git(run.worktree_path, "cherry-pick", commit)
        except RuntimeError as error:
            repository.update_integration_item(item.id, "conflict", str(error))
            repository.finish_integration_run(run.id, "needs_resolution", error=str(error))
            return repository.get_integration_run(run.id)
        repository.update_integration_item(item.id, "applied")
    result_commit = _git(run.worktree_path, "rev-parse", "HEAD")
    repository.finish_integration_run(run.id, "integrated", result_commit=result_commit)
    return repository.get_integration_run(run.id)


def verify_integration(repository: PostgresOrchestrationRepository, run_id: str, command: list[str]) -> IntegrationRun:
    run = repository.get_integration_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run.status != "integrated":
        raise ValueError("Only an integrated run can be verified")
    if not command:
        raise ValueError("command must not be empty")
    result = subprocess.run(command, cwd=run.worktree_path, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode:
        error = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        repository.finish_integration_run(run.id, "verification_failed", result_commit=run.result_commit, error=error)
    else:
        repository.finish_integration_run(run.id, "verified", result_commit=run.result_commit)
    return repository.get_integration_run(run.id)
