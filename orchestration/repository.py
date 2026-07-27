from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .models import AgentJob, AgentRole, Artifact, JobAttempt, JobEvent, JobStatus
from .state_machine import transition_is_allowed


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
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_artifact_id UUID,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (workspace_mode = 'readonly')
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
"""


def database_url_from_env() -> str | None:
    return os.getenv("PENHIN_DATABASE_URL") or None


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class PostgresOrchestrationRepository:
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

    def create_job(self, job: AgentJob) -> AgentJob:
        if job.workspace_mode != "readonly":
            raise ValueError("First-stage jobs must use readonly workspace mode")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_jobs
                (id, parent_id, root_task_id, role, subject, instruction, status, priority, workspace_mode,
                 max_turns, max_tokens, timeout_seconds)
                VALUES (%(id)s, %(parent_id)s, %(root_task_id)s, %(role)s, %(subject)s, %(instruction)s,
                        %(status)s, %(priority)s, %(workspace_mode)s, %(max_turns)s, %(max_tokens)s, %(timeout_seconds)s)""",
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
                finished_at = now() WHERE id = %s""",
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

    def _job_from_row(self, cursor, row: dict[str, Any]) -> AgentJob:
        cursor.execute("SELECT dependency_job_id FROM job_dependencies WHERE job_id = %s ORDER BY dependency_job_id", (row["id"],))
        return AgentJob(
            id=str(row["id"]), parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            root_task_id=str(row["root_task_id"]), role=AgentRole(row["role"]), subject=row["subject"], instruction=row["instruction"],
            status=JobStatus(row["status"]), priority=row["priority"], depends_on=[str(item["dependency_job_id"]) for item in cursor.fetchall()],
            workspace_mode=row["workspace_mode"], max_turns=row["max_turns"], max_tokens=row["max_tokens"], timeout_seconds=row["timeout_seconds"],
            attempt_count=row["attempt_count"], result_artifact_id=str(row["result_artifact_id"]) if row["result_artifact_id"] else None,
            error=row["error"], created_at=_time(row["created_at"]), started_at=_time(row["started_at"]), finished_at=_time(row["finished_at"]),
        )
