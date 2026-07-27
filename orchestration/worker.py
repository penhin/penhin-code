from __future__ import annotations

import argparse
import logging
import os
import sys
from uuid import uuid4

from dotenv import load_dotenv

from config import ENV_FILE
from result import Result
from runtime import init_runtime

from .artifacts import normalize_subagent_result
from .models import Artifact, JobStatus
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
        init_runtime()
        from subagent import run_subagent

        result: Result = run_subagent(job.instruction, agent_type=agent_type_for_role(str(job.role)))
        if result.ok:
            content, schema_valid = normalize_subagent_result(
                result.message,
                producer={"job_id": job.id, "role": str(job.role), "attempt_id": args.attempt_id},
            )
            artifact = Artifact(
                id=str(uuid4()), job_id=job.id, kind="agent_handoff.v1", content=content, schema_valid=schema_valid,
            )
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
