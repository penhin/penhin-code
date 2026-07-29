from __future__ import annotations

import subprocess
from uuid import uuid4

from .models import IntegrationItem, IntegrationItemStatus, IntegrationRun, IntegrationRunStatus, JobStatus
from .repositories import OrchestrationRepository
from .worktrees import provision_integration_worktree


def _git(worktree: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _change_set_for_job(repository: OrchestrationRepository, job_id: str) -> tuple[object, dict]:
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


def start_integration(repository: OrchestrationRepository, root_task_id: str, job_ids: list[str]) -> IntegrationRun:
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


def apply_integration(repository: OrchestrationRepository, run_id: str) -> IntegrationRun:
    run = repository.get_integration_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run.status != IntegrationRunStatus.CREATED:
        raise ValueError(f"Integration run {run_id} is already {run.status}")
    repository.transition_integration_run(run.id, IntegrationRunStatus.APPLYING)
    for item in repository.list_integration_items(run_id):
        if item.status == IntegrationItemStatus.APPLIED:
            continue
        repository.transition_integration_item(item.id, IntegrationItemStatus.APPLYING)
        try:
            for commit in item.commits:
                _git(run.worktree_path, "cherry-pick", commit)
        except RuntimeError as error:
            repository.transition_integration_item(item.id, IntegrationItemStatus.CONFLICT, str(error))
            repository.transition_integration_run(run.id, IntegrationRunStatus.NEEDS_RESOLUTION, error=str(error))
            return repository.get_integration_run(run.id)
        repository.transition_integration_item(item.id, IntegrationItemStatus.APPLIED)
    result_commit = _git(run.worktree_path, "rev-parse", "HEAD")
    repository.transition_integration_run(run.id, IntegrationRunStatus.INTEGRATED, result_commit=result_commit)
    return repository.get_integration_run(run.id)


def verify_integration(repository: OrchestrationRepository, run_id: str, command: list[str]) -> IntegrationRun:
    run = repository.get_integration_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run.status not in {IntegrationRunStatus.INTEGRATED, IntegrationRunStatus.VERIFICATION_FAILED}:
        raise ValueError("Only an integrated run can be verified")
    if not command:
        raise ValueError("command must not be empty")
    repository.transition_integration_run(run.id, IntegrationRunStatus.VERIFYING, result_commit=run.result_commit)
    result = subprocess.run(command, cwd=run.worktree_path, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode:
        error = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        repository.transition_integration_run(run.id, IntegrationRunStatus.VERIFICATION_FAILED, result_commit=run.result_commit, error=error)
    else:
        repository.transition_integration_run(run.id, IntegrationRunStatus.VERIFIED, result_commit=run.result_commit)
    return repository.get_integration_run(run.id)
