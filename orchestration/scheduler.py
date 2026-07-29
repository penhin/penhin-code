from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .models import AgentJob, JobAttempt, JobStatus
from .repositories import OrchestrationRepository


logger = logging.getLogger("penhin.scheduler")


def scheduler_worker_count() -> int:
    try:
        return max(1, int(os.getenv("PENHIN_SCHEDULER_WORKERS", "2")))
    except ValueError:
        return 2


def worker_kill_grace_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("PENHIN_WORKER_KILL_GRACE_SECONDS", "2")))
    except ValueError:
        return 2.0


@dataclass
class ActiveJob:
    job: AgentJob
    attempt: JobAttempt
    process: subprocess.Popen
    monitor: Future[int]
    timer: threading.Timer | None = None


class PersistentScheduler:
    """Persistent scheduler that executes every agent in a child process."""

    def __init__(self, repository: OrchestrationRepository, max_workers: int | None = None):
        self.repository = repository
        self.max_workers = max_workers or scheduler_worker_count()
        self._monitor_pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="penhin-monitor")
        self._active: dict[str, ActiveJob] = {}
        self._lock = threading.RLock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._terminate_verified_orphans()
            self.repository.recover_interrupted_jobs()
            self.repository.requeue_retryable_jobs()
            self._started = True
        self.dispatch()

    def dispatch(self) -> None:
        with self._lock:
            if not self._started:
                return
            while len(self._active) < self.max_workers:
                claimed = self.repository.claim_next_job()
                if claimed is None:
                    return
                job, attempt = claimed
                process = self._spawn_worker(job, attempt)
                monitor = self._monitor_pool.submit(process.wait)
                active = ActiveJob(job=job, attempt=attempt, process=process, monitor=monitor)
                if job.timeout_seconds:
                    timer = threading.Timer(job.timeout_seconds, self._timeout, args=(job.id, attempt.id))
                    timer.daemon = True
                    timer.start()
                    active.timer = timer
                self._active[job.id] = active
                monitor.add_done_callback(lambda completed, job_id=job.id: self._complete(job_id, completed))

    def _spawn_worker(self, job: AgentJob, attempt: JobAttempt) -> subprocess.Popen:
        environment = os.environ.copy()
        environment["PENHIN_DATABASE_URL"] = self.repository.database_url
        environment["PENHIN_WORKSPACE_MODE"] = job.workspace_mode
        if not job.worktree_path:
            raise RuntimeError(f"Worker job {job.id} has no isolated worktree")
        return subprocess.Popen(
            [
                sys.executable, "-m", "orchestration.worker",
                "--job-id", job.id,
                "--attempt-id", attempt.id,
                "--worker-token", job.worker_token,
            ],
            cwd=job.worktree_path,
            env=environment,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _timeout(self, job_id: str, attempt_id: str) -> None:
        active = self._active.get(job_id)
        if active is not None:
            self._terminate(active.process)
        try:
            self.repository.timeout_attempt(attempt_id)
        except Exception:
            logger.exception("[scheduler] timeout processing failed attempt_id=%s", attempt_id)

    def _complete(self, job_id: str, monitor: Future[int]) -> None:
        with self._lock:
            active = self._active.pop(job_id, None)
        if active is None:
            return
        if active.timer is not None:
            active.timer.cancel()
        try:
            exit_code = monitor.result()
            job = self.repository.get_job(job_id)
            if job is not None and job.status == JobStatus.RUNNING:
                self.repository.finish_attempt(
                    active.attempt.id,
                    JobStatus.FAILED,
                    error=f"Worker exited without reporting a result (exit={exit_code})",
                    terminal_reason="worker_exit",
                )
        except ValueError:
            pass
        except Exception:
            logger.exception("[scheduler] failed to reconcile worker job_id=%s", job_id)
        finally:
            self.repository.requeue_retryable_jobs()
            self.dispatch()

    def request_cancel(self, job_id: str) -> AgentJob:
        job = self.repository.request_cancel(job_id)
        if job.status == JobStatus.RUNNING:
            with self._lock:
                active = self._active.get(job_id)
            if active is not None:
                self._terminate(active.process)
        return job

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        if wait:
            for active in list(self._active.values()):
                self._terminate(active.process)
        self._monitor_pool.shutdown(wait=wait, cancel_futures=False)

    def _terminate_verified_orphans(self) -> None:
        for job_id, pid in self.repository.running_worker_processes():
            if self._belongs_to_job(pid, job_id):
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    logger.warning("[scheduler] unable to terminate orphan worker pid=%s", pid)

    @staticmethod
    def _belongs_to_job(pid: int, job_id: str) -> bool:
        try:
            command = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", errors="replace")
        except OSError:
            return False
        return "orchestration.worker" in command and job_id in command

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        timer = threading.Timer(worker_kill_grace_seconds(), PersistentScheduler._kill_if_running, args=(process,))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _kill_if_running(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
