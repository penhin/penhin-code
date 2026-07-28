from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from uuid import uuid4

from dotenv import load_dotenv

from config import ENV_FILE
from result import Result
from runtime import init_runtime

from .artifacts import normalize_subagent_result
from .models import AgentRole, Artifact, JobStatus
from .planning import DAG_PROTOCOL_VERSION, parse_dag_plan
from .repository import PostgresOrchestrationRepository


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


def finish_failure(repository: PostgresOrchestrationRepository, attempt_id: str, error: str, reason: str) -> None:
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
    if not args.database_url:
        raise RuntimeError("PENHIN_DATABASE_URL is required")
    load_dotenv(ENV_FILE, override=False)
    load_dotenv(".env", override=False)
    repository = PostgresOrchestrationRepository(args.database_url)
    repository.initialize()
    try:
        repository.register_worker_pid(args.job_id, args.attempt_id, args.worker_token, os.getpid())
        job = repository.get_job(args.job_id)
        if job is None or job.cancel_requested:
            return 0
        initial_commit = _git(job.worktree_path, "rev-parse", "HEAD") if job.workspace_mode == "isolated_write" else None
        init_runtime()
        from subagent import run_subagent

        result: Result = run_subagent(job.instruction, agent_type=agent_type_for_role(str(job.role)))
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
            else:
                content, schema_valid = normalize_subagent_result(result.message, producer=producer)
                artifact_kind = "agent_handoff.v1"
                if job.workspace_mode == "isolated_write":
                    content["change_set"] = checkpoint_change_set(job, initial_commit)
            artifact = Artifact(
                id=str(uuid4()), job_id=job.id, kind=artifact_kind, content=content, schema_valid=schema_valid,
            )
            if not schema_valid:
                repository.finish_attempt(
                    args.attempt_id,
                    JobStatus.FAILED,
                    artifact=artifact,
                    error="Agent returned an invalid structured protocol artifact",
                    terminal_reason="invalid_protocol",
                )
                return 1
            repository.finish_attempt(args.attempt_id, JobStatus.SUCCEEDED, artifact=artifact, terminal_reason="completed")
            return 0
        finish_failure(repository, args.attempt_id, result.error, result.meta.get("code", "failed"))
        return 1
    except SystemExit as error:
        finish_failure(repository, args.attempt_id, "Worker runtime configuration failed", "runtime_configuration")
        return int(error.code) if isinstance(error.code, int) else 1
    except Exception as error:
        logger.exception("worker failed job_id=%s", args.job_id)
        finish_failure(repository, args.attempt_id, str(error), "worker_error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
