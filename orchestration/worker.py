from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from uuid import uuid4

from dotenv import load_dotenv

from config import ENV_FILE
from evaluation.observer import anonymous_id, emit
from result import Result
from runtime import init_runtime

from .artifacts import build_handoff
from .models import AgentRole, Artifact, JobStatus
from .planning import DAG_PROTOCOL_VERSION, parse_dag_plan
from .repositories import OrchestrationRepository, database_url_from_env, repository_from_database_url


logger = logging.getLogger("penhin.worker")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one persistent Penhin agent job.")
    parser.add_argument("--database-url", default=os.getenv("PENHIN_DATABASE_URL", ""))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--worker-token", required=True)
    return parser.parse_args()


def agent_type_for_role(role: str) -> str:
    return {"planner": "plan", "explore": "explore", "verify": "verification"}.get(role, "general")


def finish_failure(repository: OrchestrationRepository, attempt_id: str, error: str, reason: str) -> None:
    try:
        repository.finish_attempt(attempt_id, JobStatus.FAILED, error=error, terminal_reason=reason)
    except ValueError:
        # Cancellation or timeout may have terminalized the attempt before this process observed it.
        pass


def _git(worktree: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def checkpoint_change_set(job, base_commit: str | None = None) -> dict:
    """Commit a general agent's uncommitted edits and return immutable integration metadata."""
    if job.workspace_mode != "isolated_write":
        return {"base_commit": _git(job.worktree_path, "rev-parse", "HEAD"), "commits": [], "changed_files": []}
    base_commit = base_commit or _git(job.worktree_path, "rev-parse", "HEAD")
    dirty_files = [line[3:] for line in _git(job.worktree_path, "status", "--porcelain").splitlines() if line]
    if dirty_files:
        _git(job.worktree_path, "add", "-A")
        _git(job.worktree_path, "commit", "-m", f"agent {job.id}: {job.subject[:72]}")
    commits = [item for item in _git(job.worktree_path, "rev-list", "--reverse", f"{base_commit}..HEAD").splitlines() if item]
    changed_files = [item for item in _git(job.worktree_path, "diff", "--name-only", f"{base_commit}..HEAD").splitlines() if item]
    return {"base_commit": base_commit, "commits": commits, "changed_files": changed_files}


def main() -> int:
    args = parse_args()
    load_dotenv(ENV_FILE, override=False)
    load_dotenv(".env", override=False)
    repository = repository_from_database_url(args.database_url or database_url_from_env())
    repository.initialize()
    emit("orchestration_worker_started", job_id=args.job_id, attempt_id=args.attempt_id)
    try:
        repository.register_worker_pid(args.job_id, args.attempt_id, args.worker_token, os.getpid())
        job = repository.get_job(args.job_id)
        if job is None or job.cancel_requested:
            emit(
                "orchestration_worker_aborted", job_id=args.job_id, attempt_id=args.attempt_id,
                stage="authorization", error_code="job_missing" if job is None else "cancel_requested",
            )
            return 0
        emit(
            "orchestration_worker_job_loaded", root_task_id=job.root_task_id, job_id=job.id,
            attempt_id=args.attempt_id, role=str(job.role), workspace_mode=job.workspace_mode,
            dependency_ids=job.depends_on, attempt_number=job.attempt_count,
        )
        initial_commit = _git(job.worktree_path, "rev-parse", "HEAD") if job.workspace_mode == "isolated_write" else None
        init_runtime()
        from subagent import run_subagent

        result: Result = run_subagent(job.instruction, agent_type=agent_type_for_role(str(job.role)))
        emit(
            "orchestration_agent_result", root_task_id=job.root_task_id, job_id=job.id,
            attempt_id=args.attempt_id, status="ok" if result.ok else "error",
            error_code=result.meta.get("code") if not result.ok else None,
            response_digest=anonymous_id(result.message if result.ok else result.error),
        )
        if result.ok:
            producer = {"job_id": job.id, "role": str(job.role), "attempt_id": args.attempt_id}
            if job.role == AgentRole.PLANNER:
                plan, errors = parse_dag_plan(result.message)
                content = {
                    "protocol_version": DAG_PROTOCOL_VERSION,
                    "protocol_valid": not errors,
                    "protocol_errors": errors,
                    "producer": producer,
                    "raw_text": result.message,
                    **plan,
                }
                schema_valid = not errors
                artifact_kind = "agent_dag_plan.v1"
                emit(
                    "orchestration_protocol_validated", root_task_id=job.root_task_id, job_id=job.id,
                    attempt_id=args.attempt_id, protocol="penhin.dag/v1", schema_valid=schema_valid,
                    protocol_errors=errors, response_digest=anonymous_id(result.message),
                    job_count=len(plan.get("jobs", [])) if isinstance(plan, dict) else 0,
                )
            else:
                content = build_handoff(
                    result.message,
                    producer=producer,
                    tool_results=result.meta.get("tool_results", []),
                )
                schema_valid = True
                artifact_kind = "agent_handoff.v1"
                if job.workspace_mode == "isolated_write":
                    change_set = checkpoint_change_set(job, initial_commit)
                    content["change_set"] = change_set
                    content["changed_files"] = [
                        {"path": path, "change": "modified", "detail": "Recorded in the agent change set."}
                        for path in change_set["changed_files"]
                    ]
            artifact = Artifact(
                id=str(uuid4()), job_id=job.id, kind=artifact_kind, content=content, schema_valid=schema_valid,
            )
            emit(
                "orchestration_artifact_built", root_task_id=job.root_task_id, job_id=job.id,
                attempt_id=args.attempt_id, artifact_id=artifact.id, artifact_kind=artifact.kind,
                schema_valid=artifact.schema_valid,
            )
            if not schema_valid:
                repository.finish_attempt(
                    args.attempt_id,
                    JobStatus.FAILED,
                    artifact=artifact,
                    error="Agent returned an invalid structured protocol artifact",
                    terminal_reason="invalid_protocol",
                )
                emit(
                    "orchestration_worker_completed", root_task_id=job.root_task_id, job_id=job.id,
                    attempt_id=args.attempt_id, status="failed", stage="protocol_validation",
                    error_code="invalid_protocol", protocol_errors=content.get("protocol_errors", []),
                    artifact_id=artifact.id, artifact_kind=artifact.kind,
                )
                return 1
            repository.finish_attempt(args.attempt_id, JobStatus.SUCCEEDED, artifact=artifact, terminal_reason="completed")
            emit(
                "orchestration_worker_completed", root_task_id=job.root_task_id, job_id=job.id,
                attempt_id=args.attempt_id, status="succeeded", stage="completed",
                artifact_id=artifact.id, artifact_kind=artifact_kind, schema_valid=artifact.schema_valid,
            )
            return 0
        finish_failure(repository, args.attempt_id, result.error, result.meta.get("code", "failed"))
        emit(
            "orchestration_worker_completed", root_task_id=job.root_task_id, job_id=job.id,
            attempt_id=args.attempt_id, status="failed", stage="agent_execution",
            error_code=result.meta.get("code", "failed"),
        )
        return 1
    except SystemExit as error:
        finish_failure(repository, args.attempt_id, "Worker runtime configuration failed", "runtime_configuration")
        emit(
            "orchestration_worker_completed", job_id=args.job_id, attempt_id=args.attempt_id,
            status="failed", stage="runtime_configuration", error_code="runtime_configuration",
        )
        return int(error.code) if isinstance(error.code, int) else 1
    except Exception as error:
        logger.exception("worker failed job_id=%s", args.job_id)
        finish_failure(repository, args.attempt_id, str(error), "worker_error")
        emit(
            "orchestration_worker_completed", job_id=args.job_id, attempt_id=args.attempt_id,
            status="failed", stage="worker_runtime", error_code="worker_error", error_type=type(error).__name__,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
