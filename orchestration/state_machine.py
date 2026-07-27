from __future__ import annotations

from .models import JobStatus, TERMINAL_JOB_STATUSES


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: TERMINAL_JOB_STATUSES | {JobStatus.INTERRUPTED},
    JobStatus.INTERRUPTED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.TIMED_OUT: set(),
}


def transition_is_allowed(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
