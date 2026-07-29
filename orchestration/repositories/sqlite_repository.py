from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse
from uuid import uuid4

from ..models import AgentJob, AgentRole, Artifact, IntegrationItem, IntegrationItemStatus, IntegrationRun, IntegrationRunStatus, JobAttempt, JobEvent, JobStatus
from ..state_machine import integration_item_transition_is_allowed, integration_run_transition_is_allowed, transition_is_allowed
from ..settings import sqlite_busy_timeout_ms, sqlite_connect_timeout_seconds


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES agent_jobs(id), root_task_id TEXT NOT NULL, role TEXT NOT NULL,
    subject TEXT NOT NULL, instruction TEXT NOT NULL, status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0, workspace_mode TEXT NOT NULL DEFAULT 'readonly',
    max_turns INTEGER, max_tokens INTEGER, timeout_seconds INTEGER,
    max_attempts INTEGER NOT NULL DEFAULT 1 CHECK (max_attempts >= 1), attempt_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER, worker_token TEXT NOT NULL DEFAULT '',
    result_artifact_id TEXT, error TEXT NOT NULL DEFAULT '',
    worktree_path TEXT NOT NULL DEFAULT '', worktree_branch TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT, finished_at TEXT,
    CHECK (workspace_mode IN ('readonly', 'isolated_write'))
);
CREATE TABLE IF NOT EXISTS job_dependencies (
    job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    dependency_job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT,
    PRIMARY KEY (job_id, dependency_job_id), CHECK (job_id <> dependency_job_id)
);
CREATE TABLE IF NOT EXISTS job_attempts (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    terminal_reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), finished_at TEXT,
    UNIQUE (job_id, number)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, content TEXT NOT NULL, schema_valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS job_events (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS agent_jobs_status_priority_idx ON agent_jobs (status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS agent_jobs_root_created_idx ON agent_jobs (root_task_id, created_at);
CREATE INDEX IF NOT EXISTS agent_jobs_parent_idx ON agent_jobs (parent_id);
CREATE INDEX IF NOT EXISTS job_events_job_created_idx ON job_events (job_id, created_at);
CREATE INDEX IF NOT EXISTS job_attempts_job_created_idx ON job_attempts (job_id, started_at);
CREATE INDEX IF NOT EXISTS job_dependencies_dependency_idx ON job_dependencies (dependency_job_id);
CREATE TABLE IF NOT EXISTS integration_runs (
    id TEXT PRIMARY KEY, root_task_id TEXT NOT NULL, base_commit TEXT NOT NULL,
    worktree_path TEXT NOT NULL, worktree_branch TEXT NOT NULL, status TEXT NOT NULL,
    result_commit TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), finished_at TEXT
);
CREATE TABLE IF NOT EXISTS integration_items (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES integration_runs(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT, ordinal INTEGER NOT NULL,
    source_branch TEXT NOT NULL, commits TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, job_id), UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS integration_runs_root_idx ON integration_runs (root_task_id, created_at);
CREATE INDEX IF NOT EXISTS integration_items_run_idx ON integration_items (run_id, ordinal);
"""


def sqlite_database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def sqlite_path_from_url(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite" or parsed.netloc not in {"", "localhost"} or not parsed.path:
        raise ValueError("SQLite database URL must be an absolute sqlite:/// path")
    return Path(unquote(parsed.path)).resolve()


class SqliteOrchestrationRepository:
    backend_name = "sqlite"

    def __init__(self, database_url: str):
        self.path = sqlite_path_from_url(database_url)
        self.database_url = sqlite_database_url(self.path)

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=sqlite_connect_timeout_seconds(), isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {sqlite_busy_timeout_ms()}")
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=sqlite_connect_timeout_seconds()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {sqlite_busy_timeout_ms()}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA_SQL)

    def create_root_job(self, subject: str, instruction: str, role: AgentRole = AgentRole.GENERAL) -> AgentJob:
        job_id = str(uuid4())
        return self.create_job(AgentJob(id=job_id, root_task_id=job_id, role=role, subject=subject, instruction=instruction))

    def create_root_task(self, subject: str, instruction: str) -> AgentJob:
        job_id = str(uuid4())
        return self.create_job(AgentJob(id=job_id, root_task_id=job_id, role=AgentRole.GENERAL, subject=subject, instruction=instruction, status=JobStatus.SUCCEEDED))

    def create_job(self, job: AgentJob) -> AgentJob:
        if job.workspace_mode not in {"readonly", "isolated_write"}:
            raise ValueError("workspace_mode must be readonly or isolated_write")
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO agent_jobs (id, parent_id, root_task_id, role, subject, instruction, status, priority,
                workspace_mode, max_turns, max_tokens, timeout_seconds, max_attempts, worktree_path, worktree_branch)
                VALUES (:id, :parent_id, :root_task_id, :role, :subject, :instruction, :status, :priority,
                :workspace_mode, :max_turns, :max_tokens, :timeout_seconds, :max_attempts, :worktree_path, :worktree_branch)""",
                {**job.to_dict(), "role": str(job.role), "status": str(job.status)},
            )
            for dependency_id in job.depends_on:
                connection.execute("INSERT INTO job_dependencies (job_id, dependency_job_id) VALUES (?, ?)", (job.id, dependency_id))
            self._append_event(connection, job.id, "job_created", {"role": str(job.role), "subject": job.subject})
            return self._get_job(connection, job.id)

    def get_job(self, job_id: str) -> AgentJob | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agent_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job_from_row(connection, row) if row else None

    def list_jobs(self, root_task_id: str | None = None, status: JobStatus | None = None) -> list[AgentJob]:
        clauses, values = [], []
        if root_task_id:
            clauses.append("root_task_id = ?")
            values.append(root_task_id)
        if status:
            clauses.append("status = ?")
            values.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            return [self._job_from_row(connection, row) for row in connection.execute(f"SELECT * FROM agent_jobs{where} ORDER BY created_at", values)]

    def start_attempt(self, job_id: str, model: str = "") -> JobAttempt:
        with self._connection(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if not transition_is_allowed(job.status, JobStatus.RUNNING):
                raise ValueError(f"Cannot start job in {job.status} state")
            attempt = JobAttempt(id=str(uuid4()), job_id=job_id, number=job.attempt_count + 1, model=model)
            connection.execute("UPDATE agent_jobs SET status = 'running', attempt_count = ?, started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (attempt.number, job_id))
            connection.execute("INSERT INTO job_attempts (id, job_id, number, status, model) VALUES (?, ?, ?, ?, ?)", (attempt.id, job_id, attempt.number, str(attempt.status), model))
            self._append_event(connection, job_id, "job_started", {"attempt_id": attempt.id, "number": attempt.number})
            return attempt

    def claim_next_job(self, model: str = "") -> tuple[AgentJob, JobAttempt] | None:
        with self._connection(immediate=True) as connection:
            row = connection.execute("""SELECT job.* FROM agent_jobs AS job WHERE job.status = 'queued'
                AND NOT job.cancel_requested AND job.worktree_path <> '' AND NOT EXISTS (
                    SELECT 1 FROM job_dependencies dependency JOIN agent_jobs prerequisite ON prerequisite.id = dependency.dependency_job_id
                    WHERE dependency.job_id = job.id AND prerequisite.status <> 'succeeded')
                ORDER BY job.priority DESC, job.created_at LIMIT 1""").fetchone()
            if row is None:
                return None
            job = self._job_from_row(connection, row)
            attempt = JobAttempt(id=str(uuid4()), job_id=job.id, number=job.attempt_count + 1, model=model)
            token = str(uuid4())
            connection.execute("UPDATE agent_jobs SET status = 'running', attempt_count = ?, started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), worker_token = ? WHERE id = ?", (attempt.number, token, job.id))
            connection.execute("INSERT INTO job_attempts (id, job_id, number, status, model) VALUES (?, ?, ?, 'running', ?)", (attempt.id, job.id, attempt.number, model))
            self._append_event(connection, job.id, "job_claimed", {"attempt_id": attempt.id, "number": attempt.number})
            return self._get_job(connection, job.id), attempt

    def register_worker_pid(self, job_id: str, attempt_id: str, worker_token: str, pid: int) -> None:
        with self._connection(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.status != JobStatus.RUNNING or job.worker_token != worker_token:
                raise ValueError("Worker is no longer authorized for this job")
            if connection.execute("SELECT id FROM job_attempts WHERE id = ? AND job_id = ? AND status = 'running'", (attempt_id, job_id)).fetchone() is None:
                raise ValueError("Worker attempt is no longer active")
            connection.execute("UPDATE agent_jobs SET worker_pid = ? WHERE id = ?", (pid, job_id))
            self._append_event(connection, job_id, "worker_started", {"pid": pid, "attempt_id": attempt_id})

    def running_worker_processes(self) -> list[tuple[str, int]]:
        with self._connection() as connection:
            return [(row["id"], row["worker_pid"]) for row in connection.execute("SELECT id, worker_pid FROM agent_jobs WHERE status = 'running' AND worker_pid IS NOT NULL")]

    def request_cancel(self, job_id: str) -> AgentJob:
        with self._connection(immediate=True) as connection:
            job = self._get_job(connection, job_id)
            if job.status == JobStatus.QUEUED:
                connection.execute("UPDATE agent_jobs SET status = 'cancelled', cancel_requested = 1, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), worker_pid = NULL, worker_token = '' WHERE id = ?", (job_id,))
                self._append_event(connection, job_id, "job_cancelled", {"phase": "queued"})
            elif job.status == JobStatus.RUNNING:
                connection.execute("UPDATE agent_jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
                self._append_event(connection, job_id, "job_cancel_requested", {})
            return self._get_job(connection, job_id)

    def timeout_attempt(self, attempt_id: str) -> AgentJob | None:
        with self._connection(immediate=True) as connection:
            attempt = connection.execute("SELECT * FROM job_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt is None:
                return None
            job = self._get_job(connection, attempt["job_id"])
            if job.status != JobStatus.RUNNING:
                return job
            connection.execute("UPDATE job_attempts SET status = 'timed_out', terminal_reason = 'timeout', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (attempt_id,))
            connection.execute("UPDATE agent_jobs SET status = 'timed_out', error = 'Job exceeded timeout', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), worker_pid = NULL, worker_token = '' WHERE id = ?", (job.id,))
            self._append_event(connection, job.id, "job_timed_out", {"attempt_id": attempt_id})
            return self._get_job(connection, job.id)

    def recover_interrupted_jobs(self) -> int:
        with self._connection(immediate=True) as connection:
            job_ids = [row["id"] for row in connection.execute("SELECT id FROM agent_jobs WHERE status = 'running'")]
            for job_id in job_ids:
                connection.execute("UPDATE agent_jobs SET status = 'interrupted', error = 'Scheduler restarted', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), worker_pid = NULL, worker_token = '' WHERE id = ?", (job_id,))
                connection.execute("UPDATE job_attempts SET status = 'interrupted', terminal_reason = 'scheduler_restart', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE job_id = ? AND status = 'running'", (job_id,))
                self._append_event(connection, job_id, "job_interrupted", {"reason": "scheduler_restart"})
            return len(job_ids)

    def requeue_retryable_jobs(self) -> int:
        with self._connection(immediate=True) as connection:
            job_ids = [row["id"] for row in connection.execute("SELECT id FROM agent_jobs WHERE status IN ('failed', 'interrupted') AND attempt_count < max_attempts AND NOT cancel_requested")]
            for job_id in job_ids:
                connection.execute("UPDATE agent_jobs SET status = 'queued', error = '', finished_at = NULL WHERE id = ?", (job_id,))
                self._append_event(connection, job_id, "job_requeued", {})
            return len(job_ids)

    def finish_attempt(self, attempt_id: str, status: JobStatus, *, artifact: Artifact | None = None, error: str = "", terminal_reason: str = "") -> AgentJob:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}:
            raise ValueError(f"Attempt must finish in a terminal state, got {status}")
        with self._connection(immediate=True) as connection:
            attempt = connection.execute("SELECT * FROM job_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt is None:
                raise KeyError(f"Attempt {attempt_id} not found")
            job = self._get_job(connection, attempt["job_id"])
            if job.status != JobStatus.RUNNING:
                raise ValueError(f"Cannot finish job in {job.status} state")
            if job.cancel_requested:
                status, terminal_reason = JobStatus.CANCELLED, "cancel_requested"
            artifact_id = None
            if artifact is not None:
                artifact_id = artifact.id
                connection.execute("INSERT INTO artifacts (id, job_id, kind, content, schema_valid) VALUES (?, ?, ?, ?, ?)", (artifact.id, artifact.job_id, artifact.kind, json.dumps(artifact.content), int(artifact.schema_valid)))
            connection.execute("UPDATE job_attempts SET status = ?, terminal_reason = ?, error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (str(status), terminal_reason, error, attempt_id))
            connection.execute("UPDATE agent_jobs SET status = ?, result_artifact_id = ?, error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), worker_pid = NULL, worker_token = '' WHERE id = ?", (str(status), artifact_id, error, job.id))
            self._append_event(connection, job.id, f"job_{status}", {"attempt_id": attempt_id, "error": error})
            return self._get_job(connection, job.id)

    def create_integration_run(self, run: IntegrationRun, items: list[IntegrationItem]) -> IntegrationRun:
        with self._connection(immediate=True) as connection:
            connection.execute("INSERT INTO integration_runs (id, root_task_id, base_commit, worktree_path, worktree_branch, status) VALUES (?, ?, ?, ?, ?, ?)", (run.id, run.root_task_id, run.base_commit, run.worktree_path, run.worktree_branch, str(run.status)))
            for item in items:
                connection.execute("INSERT INTO integration_items (id, run_id, job_id, ordinal, source_branch, commits, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (item.id, item.run_id, item.job_id, item.ordinal, item.source_branch, json.dumps(item.commits), str(item.status)))
            return self._get_integration_run(connection, run.id)

    def get_integration_run(self, run_id: str) -> IntegrationRun | None:
        with self._connection() as connection:
            return self._get_integration_run(connection, run_id, required=False)

    def list_integration_items(self, run_id: str) -> list[IntegrationItem]:
        with self._connection() as connection:
            return [self._integration_item_from_row(row) for row in connection.execute("SELECT * FROM integration_items WHERE run_id = ? ORDER BY ordinal", (run_id,))]

    def transition_integration_item(self, item_id: str, status: IntegrationItemStatus, error: str = "") -> None:
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM integration_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            current = IntegrationItemStatus(row["status"])
            if not integration_item_transition_is_allowed(current, status):
                raise ValueError(f"Cannot transition integration item from {current} to {status}")
            connection.execute("UPDATE integration_items SET status = ?, error = ? WHERE id = ?", (str(status), error, item_id))

    def transition_integration_run(self, run_id: str, status: IntegrationRunStatus, result_commit: str = "", error: str = "") -> None:
        with self._connection(immediate=True) as connection:
            run = self._get_integration_run(connection, run_id)
            if not integration_run_transition_is_allowed(run.status, status):
                raise ValueError(f"Cannot transition integration run from {run.status} to {status}")
            connection.execute("UPDATE integration_runs SET status = ?, result_commit = ?, error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (str(status), result_commit, error, run_id))

    def list_events(self, job_id: str) -> list[JobEvent]:
        with self._connection() as connection:
            return [JobEvent(id=row["id"], job_id=row["job_id"], event_type=row["event_type"], payload=json.loads(row["payload"]), created_at=row["created_at"]) for row in connection.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY created_at", (job_id,))]

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            return Artifact(id=row["id"], job_id=row["job_id"], kind=row["kind"], content=json.loads(row["content"]), schema_valid=bool(row["schema_valid"]), created_at=row["created_at"]) if row else None

    @staticmethod
    def _append_event(connection: sqlite3.Connection, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        connection.execute("INSERT INTO job_events (id, job_id, event_type, payload) VALUES (?, ?, ?, ?)", (str(uuid4()), job_id, event_type, json.dumps(payload)))

    def _get_job(self, connection: sqlite3.Connection, job_id: str) -> AgentJob:
        row = connection.execute("SELECT * FROM agent_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} not found")
        return self._job_from_row(connection, row)

    def _get_integration_run(self, connection: sqlite3.Connection, run_id: str, required: bool = True) -> IntegrationRun | None:
        row = connection.execute("SELECT * FROM integration_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            if required:
                raise KeyError(run_id)
            return None
        return IntegrationRun(id=row["id"], root_task_id=row["root_task_id"], base_commit=row["base_commit"], worktree_path=row["worktree_path"], worktree_branch=row["worktree_branch"], status=IntegrationRunStatus(row["status"]), result_commit=row["result_commit"], error=row["error"], created_at=row["created_at"], finished_at=row["finished_at"])

    @staticmethod
    def _integration_item_from_row(row: sqlite3.Row) -> IntegrationItem:
        return IntegrationItem(id=row["id"], run_id=row["run_id"], job_id=row["job_id"], ordinal=row["ordinal"], source_branch=row["source_branch"], commits=json.loads(row["commits"]), status=IntegrationItemStatus(row["status"]), error=row["error"])

    def _job_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> AgentJob:
        depends_on = [item["dependency_job_id"] for item in connection.execute("SELECT dependency_job_id FROM job_dependencies WHERE job_id = ? ORDER BY dependency_job_id", (row["id"],))]
        return AgentJob(id=row["id"], parent_id=row["parent_id"], root_task_id=row["root_task_id"], role=AgentRole(row["role"]), subject=row["subject"], instruction=row["instruction"], status=JobStatus(row["status"]), priority=row["priority"], depends_on=depends_on, workspace_mode=row["workspace_mode"], worktree_path=row["worktree_path"], worktree_branch=row["worktree_branch"], max_turns=row["max_turns"], max_tokens=row["max_tokens"], timeout_seconds=row["timeout_seconds"], max_attempts=row["max_attempts"], attempt_count=row["attempt_count"], cancel_requested=bool(row["cancel_requested"]), worker_pid=row["worker_pid"], worker_token=row["worker_token"], result_artifact_id=row["result_artifact_id"], error=row["error"], created_at=row["created_at"], started_at=row["started_at"], finished_at=row["finished_at"])
