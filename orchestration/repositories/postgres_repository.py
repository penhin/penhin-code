from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from ..models import AgentJob, AgentRole, Artifact, IntegrationItem, IntegrationItemStatus, IntegrationRun, IntegrationRunStatus, JobAttempt, JobEvent, JobStatus
from ..state_machine import integration_item_transition_is_allowed, integration_run_transition_is_allowed, transition_is_allowed


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    id UUID PRIMARY KEY,
    parent_id UUID REFERENCES agent_jobs(id),
    root_task_id UUID NOT NULL,
    role TEXT NOT NULL,
    subject TEXT NOT NULL,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    workspace_mode TEXT NOT NULL DEFAULT 'readonly',
    max_turns INTEGER,
    max_tokens INTEGER,
    timeout_seconds INTEGER,
    max_attempts INTEGER NOT NULL DEFAULT 1 CHECK (max_attempts >= 1),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    worker_pid INTEGER,
    worker_token TEXT NOT NULL DEFAULT '',
    result_artifact_id UUID,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (workspace_mode IN ('readonly', 'isolated_write'))
);
CREATE TABLE IF NOT EXISTS job_dependencies (
    job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    dependency_job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT,
    PRIMARY KEY (job_id, dependency_job_id),
    CHECK (job_id <> dependency_job_id)
);
CREATE TABLE IF NOT EXISTS job_attempts (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    terminal_reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (job_id, number)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content JSONB NOT NULL,
    schema_valid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS job_events (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_jobs_status_priority_idx ON agent_jobs (status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS agent_jobs_root_created_idx ON agent_jobs (root_task_id, created_at);
CREATE INDEX IF NOT EXISTS agent_jobs_parent_idx ON agent_jobs (parent_id);
CREATE INDEX IF NOT EXISTS job_events_job_created_idx ON job_events (job_id, created_at);
CREATE INDEX IF NOT EXISTS job_attempts_job_created_idx ON job_attempts (job_id, started_at);
CREATE INDEX IF NOT EXISTS job_dependencies_dependency_idx ON job_dependencies (dependency_job_id);
CREATE TABLE IF NOT EXISTS integration_runs (
    id UUID PRIMARY KEY,
    root_task_id UUID NOT NULL,
    base_commit TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    worktree_branch TEXT NOT NULL,
    status TEXT NOT NULL,
    result_commit TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS integration_items (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES integration_runs(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    source_branch TEXT NOT NULL,
    commits JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, job_id),
    UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS integration_runs_root_idx ON integration_runs (root_task_id, created_at);
CREATE INDEX IF NOT EXISTS integration_items_run_idx ON integration_items (run_id, ordinal);
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS worker_pid INTEGER;
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS worker_token TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS worktree_path TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS worktree_branch TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_jobs DROP CONSTRAINT IF EXISTS agent_jobs_workspace_mode_check;
ALTER TABLE agent_jobs ADD CONSTRAINT agent_jobs_workspace_mode_check CHECK (workspace_mode IN ('readonly', 'isolated_write'));
"""


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class PostgresOrchestrationRepository:
    backend_name = "postgresql"

    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def initialize(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)

    def create_root_job(self, subject: str, instruction: str, role: AgentRole = AgentRole.GENERAL) -> AgentJob:
        job_id = str(uuid4())
        return self.create_job(AgentJob(
            id=job_id,
            root_task_id=job_id,
            role=role,
            subject=subject,
            instruction=instruction,
        ))

    def create_root_task(self, subject: str, instruction: str) -> AgentJob:
        """Create a completed coordination root that is never claimed by a Worker."""
        job_id = str(uuid4())
        return self.create_job(AgentJob(
            id=job_id,
            root_task_id=job_id,
            role=AgentRole.GENERAL,
            subject=subject,
            instruction=instruction,
            status=JobStatus.SUCCEEDED,
        ))

    def create_job(self, job: AgentJob) -> AgentJob:
        if job.workspace_mode not in {"readonly", "isolated_write"}:
            raise ValueError("workspace_mode must be readonly or isolated_write")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_jobs
                (id, parent_id, root_task_id, role, subject, instruction, status, priority, workspace_mode,
                 max_turns, max_tokens, timeout_seconds, max_attempts, worktree_path, worktree_branch)
                VALUES (%(id)s, %(parent_id)s, %(root_task_id)s, %(role)s, %(subject)s, %(instruction)s,
                        %(status)s, %(priority)s, %(workspace_mode)s, %(max_turns)s, %(max_tokens)s, %(timeout_seconds)s, %(max_attempts)s, %(worktree_path)s, %(worktree_branch)s)""",
                {**job.to_dict(), "role": str(job.role), "status": str(job.status)},
            )
            for dependency_id in job.depends_on:
                cursor.execute(
                    "INSERT INTO job_dependencies (job_id, dependency_job_id) VALUES (%s, %s)",
                    (job.id, dependency_id),
                )
            self._append_event(cursor, job.id, "job_created", {"role": str(job.role), "subject": job.subject})
            return self._get_job(cursor, job.id)

    def get_job(self, job_id: str) -> AgentJob | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM agent_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            return self._job_from_row(cursor, row) if row else None

    def list_jobs(self, root_task_id: str | None = None, status: JobStatus | None = None) -> list[AgentJob]:
        clauses, values = [], []
        if root_task_id:
            clauses.append("root_task_id = %s")
            values.append(root_task_id)
        if status:
            clauses.append("status = %s")
            values.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM agent_jobs{where} ORDER BY created_at", values)
            return [self._job_from_row(cursor, row) for row in cursor.fetchall()]

    def create_integration_run(self, run: IntegrationRun, items: list[IntegrationItem]) -> IntegrationRun:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO integration_runs (id, root_task_id, base_commit, worktree_path, worktree_branch, status)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run.id, run.root_task_id, run.base_commit, run.worktree_path, run.worktree_branch, str(run.status)),
            )
            for item in items:
                cursor.execute(
                    """INSERT INTO integration_items (id, run_id, job_id, ordinal, source_branch, commits, status)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
                    (item.id, item.run_id, item.job_id, item.ordinal, item.source_branch, json.dumps(item.commits), str(item.status)),
                )
            return self._get_integration_run(cursor, run.id)

    def get_integration_run(self, run_id: str) -> IntegrationRun | None:
        with self._connection() as connection, connection.cursor() as cursor:
            return self._get_integration_run(cursor, run_id, required=False)

    def list_integration_items(self, run_id: str) -> list[IntegrationItem]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM integration_items WHERE run_id = %s ORDER BY ordinal", (run_id,))
            return [self._integration_item_from_row(row) for row in cursor.fetchall()]

    def transition_integration_item(self, item_id: str, status: IntegrationItemStatus, error: str = "") -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM integration_items WHERE id = %s FOR UPDATE", (item_id,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(item_id)
            current = IntegrationItemStatus(row["status"])
            if not integration_item_transition_is_allowed(current, status):
                raise ValueError(f"Cannot transition integration item from {current} to {status}")
            cursor.execute("UPDATE integration_items SET status = %s, error = %s WHERE id = %s", (str(status), error, item_id))

    def transition_integration_run(self, run_id: str, status: IntegrationRunStatus, result_commit: str = "", error: str = "") -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            run = self._get_integration_run(cursor, run_id)
            if not integration_run_transition_is_allowed(run.status, status):
                raise ValueError(f"Cannot transition integration run from {run.status} to {status}")
            cursor.execute(
                "UPDATE integration_runs SET status = %s, result_commit = %s, error = %s, finished_at = now() WHERE id = %s",
                (str(status), result_commit, error, run_id),
            )

    def start_attempt(self, job_id: str, model: str = "") -> JobAttempt:
        with self._connection() as connection, connection.cursor() as cursor:
            job = self._get_job(cursor, job_id, for_update=True)
            if not transition_is_allowed(job.status, JobStatus.RUNNING):
                raise ValueError(f"Cannot start job in {job.status} state")
            number = job.attempt_count + 1
            attempt = JobAttempt(id=str(uuid4()), job_id=job_id, number=number, model=model)
            cursor.execute(
                "UPDATE agent_jobs SET status = 'running', attempt_count = %s, started_at = now() WHERE id = %s",
                (number, job_id),
            )
            cursor.execute(
                "INSERT INTO job_attempts (id, job_id, number, status, model) VALUES (%s, %s, %s, %s, %s)",
                (attempt.id, job_id, number, str(attempt.status), model),
            )
            self._append_event(cursor, job_id, "job_started", {"attempt_id": attempt.id, "number": number})
            return attempt

    def claim_next_job(self, model: str = "") -> tuple[AgentJob, JobAttempt] | None:
        """Atomically claim one dependency-ready job; safe for multiple scheduler processes."""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT job.* FROM agent_jobs AS job
                WHERE job.status = 'queued' AND NOT job.cancel_requested AND job.worktree_path <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM job_dependencies dependency
                    JOIN agent_jobs prerequisite ON prerequisite.id = dependency.dependency_job_id
                    WHERE dependency.job_id = job.id AND prerequisite.status <> 'succeeded'
                  )
                ORDER BY job.priority DESC, job.created_at
                FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            row = cursor.fetchone()
            if row is None:
                return None
            job = self._job_from_row(cursor, row)
            number = job.attempt_count + 1
            attempt = JobAttempt(id=str(uuid4()), job_id=job.id, number=number, model=model)
            worker_token = str(uuid4())
            cursor.execute(
                "UPDATE agent_jobs SET status = 'running', attempt_count = %s, started_at = now(), worker_token = %s WHERE id = %s",
                (number, worker_token, job.id),
            )
            cursor.execute(
                "INSERT INTO job_attempts (id, job_id, number, status, model) VALUES (%s, %s, %s, 'running', %s)",
                (attempt.id, job.id, number, model),
            )
            self._append_event(cursor, job.id, "job_claimed", {"attempt_id": attempt.id, "number": number})
            return self._get_job(cursor, job.id), attempt

    def register_worker_pid(self, job_id: str, attempt_id: str, worker_token: str, pid: int) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            job = self._get_job(cursor, job_id, for_update=True)
            if job.status != JobStatus.RUNNING or job.worker_token != worker_token:
                raise ValueError("Worker is no longer authorized for this job")
            cursor.execute("SELECT id FROM job_attempts WHERE id = %s AND job_id = %s AND status = 'running'", (attempt_id, job_id))
            if cursor.fetchone() is None:
                raise ValueError("Worker attempt is no longer active")
            cursor.execute("UPDATE agent_jobs SET worker_pid = %s WHERE id = %s", (pid, job_id))
            self._append_event(cursor, job_id, "worker_started", {"pid": pid, "attempt_id": attempt_id})

    def running_worker_processes(self) -> list[tuple[str, int]]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, worker_pid FROM agent_jobs WHERE status = 'running' AND worker_pid IS NOT NULL")
            return [(str(row["id"]), int(row["worker_pid"])) for row in cursor.fetchall()]

    def request_cancel(self, job_id: str) -> AgentJob:
        with self._connection() as connection, connection.cursor() as cursor:
            job = self._get_job(cursor, job_id, for_update=True)
            if job.status == JobStatus.QUEUED:
                cursor.execute("UPDATE agent_jobs SET status = 'cancelled', cancel_requested = TRUE, finished_at = now(), worker_pid = NULL, worker_token = '' WHERE id = %s", (job_id,))
                self._append_event(cursor, job_id, "job_cancelled", {"phase": "queued"})
            elif job.status == JobStatus.RUNNING:
                cursor.execute("UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = %s", (job_id,))
                self._append_event(cursor, job_id, "job_cancel_requested", {})
            return self._get_job(cursor, job_id)

    def timeout_attempt(self, attempt_id: str) -> AgentJob | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM job_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
            attempt = cursor.fetchone()
            if attempt is None:
                return None
            job = self._get_job(cursor, str(attempt["job_id"]), for_update=True)
            if job.status != JobStatus.RUNNING:
                return job
            cursor.execute("UPDATE job_attempts SET status = 'timed_out', terminal_reason = 'timeout', finished_at = now() WHERE id = %s", (attempt_id,))
            cursor.execute("UPDATE agent_jobs SET status = 'timed_out', error = 'Job exceeded timeout', finished_at = now(), worker_pid = NULL, worker_token = '' WHERE id = %s", (job.id,))
            self._append_event(cursor, job.id, "job_timed_out", {"attempt_id": attempt_id})
            return self._get_job(cursor, job.id)

    def recover_interrupted_jobs(self) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM agent_jobs WHERE status = 'running' FOR UPDATE")
            job_ids = [str(row["id"]) for row in cursor.fetchall()]
            for job_id in job_ids:
                cursor.execute("UPDATE agent_jobs SET status = 'interrupted', error = 'Scheduler restarted', finished_at = now(), worker_pid = NULL, worker_token = '' WHERE id = %s", (job_id,))
                cursor.execute("UPDATE job_attempts SET status = 'interrupted', terminal_reason = 'scheduler_restart', finished_at = now() WHERE job_id = %s AND status = 'running'", (job_id,))
                self._append_event(cursor, job_id, "job_interrupted", {"reason": "scheduler_restart"})
            return len(job_ids)

    def requeue_retryable_jobs(self) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM agent_jobs WHERE status IN ('failed', 'interrupted') AND attempt_count < max_attempts AND NOT cancel_requested FOR UPDATE")
            job_ids = [str(row["id"]) for row in cursor.fetchall()]
            for job_id in job_ids:
                cursor.execute("UPDATE agent_jobs SET status = 'queued', error = '', finished_at = NULL WHERE id = %s", (job_id,))
                self._append_event(cursor, job_id, "job_requeued", {})
            return len(job_ids)

    def finish_attempt(
        self,
        attempt_id: str,
        status: JobStatus,
        *,
        artifact: Artifact | None = None,
        error: str = "",
        terminal_reason: str = "",
    ) -> AgentJob:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}:
            raise ValueError(f"Attempt must finish in a terminal state, got {status}")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM job_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
            attempt = cursor.fetchone()
            if attempt is None:
                raise KeyError(f"Attempt {attempt_id} not found")
            job = self._get_job(cursor, str(attempt["job_id"]), for_update=True)
            if job.status != JobStatus.RUNNING:
                raise ValueError(f"Cannot finish job in {job.status} state")
            if job.cancel_requested:
                status = JobStatus.CANCELLED
                terminal_reason = "cancel_requested"
            artifact_id = None
            if artifact is not None:
                artifact_id = artifact.id
                cursor.execute(
                    "INSERT INTO artifacts (id, job_id, kind, content, schema_valid) VALUES (%s, %s, %s, %s, %s)",
                    (artifact.id, artifact.job_id, artifact.kind, psycopg.types.json.Jsonb(artifact.content), artifact.schema_valid),
                )
            cursor.execute(
                "UPDATE job_attempts SET status = %s, terminal_reason = %s, error = %s, finished_at = now() WHERE id = %s",
                (str(status), terminal_reason, error, attempt_id),
            )
            cursor.execute(
                """UPDATE agent_jobs SET status = %s, result_artifact_id = %s, error = %s,
                finished_at = now(), worker_pid = NULL, worker_token = '' WHERE id = %s""",
                (str(status), artifact_id, error, job.id),
            )
            self._append_event(cursor, job.id, f"job_{status}", {"attempt_id": attempt_id, "error": error})
            return self._get_job(cursor, job.id)

    def list_events(self, job_id: str) -> list[JobEvent]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM job_events WHERE job_id = %s ORDER BY created_at", (job_id,))
            return [JobEvent(id=str(row["id"]), job_id=str(row["job_id"]), event_type=row["event_type"], payload=row["payload"], created_at=_time(row["created_at"])) for row in cursor.fetchall()]

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return Artifact(id=str(row["id"]), job_id=str(row["job_id"]), kind=row["kind"], content=row["content"], schema_valid=row["schema_valid"], created_at=_time(row["created_at"]))

    def _append_event(self, cursor, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        cursor.execute(
            "INSERT INTO job_events (id, job_id, event_type, payload) VALUES (%s, %s, %s, %s)",
            (str(uuid4()), job_id, event_type, psycopg.types.json.Jsonb(payload)),
        )

    def _get_job(self, cursor, job_id: str, for_update: bool = False) -> AgentJob:
        cursor.execute(f"SELECT * FROM agent_jobs WHERE id = %s{' FOR UPDATE' if for_update else ''}", (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} not found")
        return self._job_from_row(cursor, row)

    def _get_integration_run(self, cursor, run_id: str, required: bool = True) -> IntegrationRun | None:
        cursor.execute("SELECT * FROM integration_runs WHERE id = %s", (run_id,))
        row = cursor.fetchone()
        if row is None:
            if required:
                raise KeyError(run_id)
            return None
        return IntegrationRun(
            id=str(row["id"]), root_task_id=str(row["root_task_id"]), base_commit=row["base_commit"],
            worktree_path=row["worktree_path"], worktree_branch=row["worktree_branch"], status=IntegrationRunStatus(row["status"]),
            result_commit=row["result_commit"], error=row["error"], created_at=_time(row["created_at"]),
            finished_at=_time(row["finished_at"]),
        )

    @staticmethod
    def _integration_item_from_row(row: dict[str, Any]) -> IntegrationItem:
        return IntegrationItem(
            id=str(row["id"]), run_id=str(row["run_id"]), job_id=str(row["job_id"]), ordinal=row["ordinal"],
            source_branch=row["source_branch"], commits=list(row["commits"]), status=IntegrationItemStatus(row["status"]), error=row["error"],
        )

    def _job_from_row(self, cursor, row: dict[str, Any]) -> AgentJob:
        cursor.execute("SELECT dependency_job_id FROM job_dependencies WHERE job_id = %s ORDER BY dependency_job_id", (row["id"],))
        return AgentJob(
            id=str(row["id"]), parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            root_task_id=str(row["root_task_id"]), role=AgentRole(row["role"]), subject=row["subject"], instruction=row["instruction"],
            status=JobStatus(row["status"]), priority=row["priority"], depends_on=[str(item["dependency_job_id"]) for item in cursor.fetchall()],
            workspace_mode=row["workspace_mode"], worktree_path=row["worktree_path"], worktree_branch=row["worktree_branch"], max_turns=row["max_turns"], max_tokens=row["max_tokens"], timeout_seconds=row["timeout_seconds"], max_attempts=row["max_attempts"],
            attempt_count=row["attempt_count"], cancel_requested=row["cancel_requested"], worker_pid=row["worker_pid"], worker_token=row["worker_token"], result_artifact_id=str(row["result_artifact_id"]) if row["result_artifact_id"] else None,
            error=row["error"], created_at=_time(row["created_at"]), started_at=_time(row["started_at"]), finished_at=_time(row["finished_at"]),
        )
