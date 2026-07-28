from __future__ import annotations

from .models import IntegrationItemStatus, IntegrationRunStatus, JobStatus, TERMINAL_JOB_STATUSES


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


INTEGRATION_RUN_TRANSITIONS: dict[IntegrationRunStatus, set[IntegrationRunStatus]] = {
    IntegrationRunStatus.CREATED: {IntegrationRunStatus.APPLYING},
    IntegrationRunStatus.APPLYING: {IntegrationRunStatus.NEEDS_RESOLUTION, IntegrationRunStatus.INTEGRATED},
    IntegrationRunStatus.NEEDS_RESOLUTION: {IntegrationRunStatus.APPLYING},
    IntegrationRunStatus.INTEGRATED: {IntegrationRunStatus.VERIFYING},
    IntegrationRunStatus.VERIFYING: {IntegrationRunStatus.VERIFIED, IntegrationRunStatus.VERIFICATION_FAILED},
    IntegrationRunStatus.VERIFICATION_FAILED: {IntegrationRunStatus.VERIFYING},
    IntegrationRunStatus.VERIFIED: set(),
}

INTEGRATION_ITEM_TRANSITIONS: dict[IntegrationItemStatus, set[IntegrationItemStatus]] = {
    IntegrationItemStatus.PENDING: {IntegrationItemStatus.APPLYING},
    IntegrationItemStatus.APPLYING: {IntegrationItemStatus.APPLIED, IntegrationItemStatus.CONFLICT},
    IntegrationItemStatus.CONFLICT: {IntegrationItemStatus.APPLYING},
    IntegrationItemStatus.APPLIED: set(),
}


def integration_run_transition_is_allowed(current: IntegrationRunStatus, target: IntegrationRunStatus) -> bool:
    return target in INTEGRATION_RUN_TRANSITIONS[current]


def integration_item_transition_is_allowed(current: IntegrationItemStatus, target: IntegrationItemStatus) -> bool:
    return target in INTEGRATION_ITEM_TRANSITIONS[current]
