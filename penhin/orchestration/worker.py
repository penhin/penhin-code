from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from penhin.auth.secrets import redact_text, safe_value
from penhin.infrastructure.config import ENV_FILE
from penhin.evaluation.observer import anonymous_id, emit
from penhin.result import Result
from penhin.runtime import runtime_manager

from .artifacts import build_handoff
from .models import AgentRole, Artifact, JobStatus
from .planning import DAG_PROTOCOL_VERSION, dag_protocol_instructions, fallback_dag_plan, normalize_dag_plan, parse_dag_plan
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
        repository.finish_attempt(attempt_id, JobStatus.FAILED, error=redact_text(error), terminal_reason=reason)
    except ValueError:
        # Cancellation or timeout may have terminalized the attempt before this process observed it.
        pass


def _git(worktree: str, *args: str) -> str:
    from penhin.auth.secrets import scrubbed_environment
    result = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment())
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


def dependency_jobs_in_order(repository: OrchestrationRepository, job) -> list:
    ordered = []
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visited:
            return
        dependency = repository.get_job(job_id)
        if dependency is None:
            raise ValueError(f"Dependency job {job_id} does not exist")
        if dependency.status != JobStatus.SUCCEEDED:
            raise ValueError(f"Dependency job {job_id} is {dependency.status}, expected succeeded")
        for parent_id in dependency.depends_on:
            visit(parent_id)
        visited.add(job_id)
        ordered.append(dependency)

    for dependency_id in job.depends_on:
        visit(dependency_id)
    return ordered


def prepare_dependency_context(repository: OrchestrationRepository, job) -> tuple[list[dict], list[str]]:
    dependencies = dependency_jobs_in_order(repository, job)
    context: list[dict] = []
    applied_commits: list[str] = []
    emit(
        "orchestration_dependency_prepare_started", root_task_id=job.root_task_id, job_id=job.id,
        dependency_job_ids=[dependency.id for dependency in dependencies],
    )
    for dependency in dependencies:
        artifact = repository.get_artifact(dependency.result_artifact_id) if dependency.result_artifact_id else None
        if artifact is None or not artifact.schema_valid:
            raise ValueError(f"Dependency job {dependency.id} has no valid artifact")
        change_set = artifact.content.get("change_set")
        if isinstance(change_set, dict):
            for commit in change_set.get("commits", []):
                if not isinstance(commit, str) or not commit or commit in applied_commits:
                    continue
                try:
                    _git(job.worktree_path, "cherry-pick", commit)
                except RuntimeError:
                    from penhin.auth.secrets import scrubbed_environment
                    subprocess.run(
                        ["git", "cherry-pick", "--abort"], cwd=job.worktree_path,
                        capture_output=True, text=True, timeout=30, check=False, env=scrubbed_environment(),
                    )
                    emit(
                        "orchestration_dependency_integration_failed", root_task_id=job.root_task_id,
                        job_id=job.id, dependency_job_id=dependency.id,
                        artifact_id=artifact.id, stage="dependency_integration",
                        error_code="dependency_integration_conflict",
                    )
                    raise
                applied_commits.append(commit)
        context.append({
            "job_id": dependency.id,
            "role": str(dependency.role),
            "artifact_id": artifact.id,
            "artifact_kind": artifact.kind,
            "summary": artifact.content.get("summary", ""),
            "findings": artifact.content.get("findings", []),
            "risks": artifact.content.get("risks", []),
            "changed_files": artifact.content.get("changed_files", []),
        })
    emit(
        "orchestration_dependency_prepare_completed", root_task_id=job.root_task_id, job_id=job.id,
        dependency_job_ids=[dependency.id for dependency in dependencies],
        dependency_artifact_ids=[item["artifact_id"] for item in context],
        applied_commit_count=len(applied_commits),
    )
    return context, applied_commits


def repair_dag_plan(goal: str, invalid_response: str, errors: list[str]) -> tuple[dict, list[str], str]:
    runtime = runtime_manager.current()
    payload = json.dumps({
        "goal": goal,
        "validation_errors": errors,
        "invalid_response": invalid_response[:12000],
    }, ensure_ascii=False)
    response = runtime.call_with_retry(
        system=(
            "Repair an invalid DAG plan. Return one corrected JSON object only. "
            "Do not call tools, use Markdown, or include explanations.\n\n" + dag_protocol_instructions()
        ),
        messages=[{"role": "user", "content": payload}],
        max_tokens=min(runtime.sub_max_tokens, 4000),
    )
    text = "\n".join(
        str(block.get("text", ""))
        for block in response.content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    plan, repair_errors = parse_dag_plan(text)
    return plan, repair_errors, text


def claim_invalid_artifact_injection(job) -> bool:
    if os.getenv("PENHIN_EVAL_FAULT") != "invalid_artifact_once" or job.role == AgentRole.PLANNER:
        return False
    run_dir = os.getenv("PENHIN_EVAL_RUN_DIR", "")
    if not run_dir:
        return False
    marker = Path(run_dir) / "injections" / (
        f"{os.getenv('PENHIN_EVAL_CASE_ID', 'case')}-{os.getenv('PENHIN_EVAL_REPETITION', '0')}-invalid-artifact"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


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
        root_base_commit = _git(job.worktree_path, "rev-parse", "HEAD")
        dependency_context, applied_dependency_commits = prepare_dependency_context(repository, job)
        initial_commit = _git(job.worktree_path, "rev-parse", "HEAD") if job.workspace_mode == "isolated_write" else None
        runtime_manager.initialize()
        from penhin.agent.subagents.service import run_subagent

        instruction = job.instruction
        if dependency_context:
            instruction += (
                "\n\nUse these completed dependency artifacts as authoritative upstream context. "
                "Do not repeat their broad exploration.\n<dependency_artifacts>\n"
                + json.dumps(dependency_context, ensure_ascii=False)
                + "\n</dependency_artifacts>"
            )
        result: Result = run_subagent(instruction, agent_type=agent_type_for_role(str(job.role)))
        forced_plan_source = ""
        if (
            job.role == AgentRole.PLANNER
            and not result.ok
            and result.meta.get("code") in {"summary_failed", "max_turns", "tool_budget_exhausted"}
        ):
            fallback = fallback_dag_plan(job.instruction)
            forced_plan_source = "execution_fallback"
            emit(
                "orchestration_planner_execution_fallback_used", root_task_id=job.root_task_id,
                job_id=job.id, attempt_id=args.attempt_id,
                original_error_code=result.meta.get("code"), job_count=len(fallback["jobs"]),
            )
            result = Result.success(json.dumps(fallback, ensure_ascii=False))
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
                source = forced_plan_source or "model"
                emit(
                    "orchestration_protocol_validated", root_task_id=job.root_task_id, job_id=job.id,
                    attempt_id=args.attempt_id, protocol="penhin.dag/v1", validation_attempt=1,
                    schema_valid=not errors, protocol_errors=errors, response_digest=anonymous_id(result.message),
                    job_count=len(plan.get("jobs", [])) if isinstance(plan, dict) else 0,
                )
                if errors:
                    try:
                        repaired, repair_errors, repaired_text = repair_dag_plan(job.instruction, result.message, errors)
                        emit(
                            "orchestration_protocol_validated", root_task_id=job.root_task_id, job_id=job.id,
                            attempt_id=args.attempt_id, protocol="penhin.dag/v1", validation_attempt=2,
                            schema_valid=not repair_errors, protocol_errors=repair_errors,
                            response_digest=anonymous_id(repaired_text),
                            job_count=len(repaired.get("jobs", [])) if isinstance(repaired, dict) else 0,
                        )
                        if not repair_errors:
                            plan, errors, source = repaired, [], "repair"
                    except Exception as repair_error:
                        emit(
                            "orchestration_protocol_repair_failed", root_task_id=job.root_task_id, job_id=job.id,
                            attempt_id=args.attempt_id, error_type=type(repair_error).__name__,
                        )
                if errors:
                    plan, errors, source = fallback_dag_plan(job.instruction), [], "deterministic_fallback"
                    emit(
                        "orchestration_protocol_fallback_used", root_task_id=job.root_task_id, job_id=job.id,
                        attempt_id=args.attempt_id, protocol="penhin.dag/v1", job_count=len(plan["jobs"]),
                    )
                plan, semantic_changes = normalize_dag_plan(plan, job.instruction)
                if semantic_changes:
                    emit(
                        "orchestration_plan_normalized", root_task_id=job.root_task_id, job_id=job.id,
                        attempt_id=args.attempt_id, changes=semantic_changes,
                    )
                content = {
                    "protocol_version": DAG_PROTOCOL_VERSION,
                    "protocol_valid": not errors,
                    "protocol_errors": errors,
                    "producer": producer,
                    "raw_text": result.message,
                    "plan_source": source,
                    "semantic_normalizations": semantic_changes,
                    **plan,
                }
                schema_valid = not errors
                artifact_kind = "agent_dag_plan.v1"
            else:
                content = build_handoff(
                    result.message,
                    producer=producer,
                    tool_results=result.meta.get("tool_results", []),
                )
                schema_valid = True
                artifact_kind = "agent_handoff.v1"
                content["dependency_job_ids"] = [item["job_id"] for item in dependency_context]
                content["dependency_artifact_ids"] = [item["artifact_id"] for item in dependency_context]
                content["applied_dependency_commits"] = applied_dependency_commits
                if job.workspace_mode == "isolated_write":
                    change_set = checkpoint_change_set(job, initial_commit)
                    if applied_dependency_commits:
                        change_set["base_commit"] = root_base_commit
                        change_set["commits"] = [*applied_dependency_commits, *change_set["commits"]]
                        change_set["changed_files"] = [
                            item for item in _git(job.worktree_path, "diff", "--name-only", f"{root_base_commit}..HEAD").splitlines() if item
                        ]
                    content["change_set"] = change_set
                    content["changed_files"] = [
                        {"path": path, "change": "modified", "detail": "Recorded in the agent change set."}
                        for path in change_set["changed_files"]
                    ]
            artifact = Artifact(
                id=str(uuid4()), job_id=job.id, kind=artifact_kind,
                content=safe_value(content), schema_valid=schema_valid,
            )
            if claim_invalid_artifact_injection(job):
                artifact.schema_valid = False
                artifact.content["protocol_valid"] = False
                artifact.content["protocol_errors"] = ["deterministic evaluation fault: invalid artifact"]
                schema_valid = False
                emit(
                    "orchestration_fault_injected", root_task_id=job.root_task_id, job_id=job.id,
                    attempt_id=args.attempt_id, fault="invalid_artifact", artifact_id=artifact.id,
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
