from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context import RunContext


class PermissionMode(str, Enum):
    DEFAULT = "default"
    AUTO_REVIEW = "auto-review"
    FULL_ACCESS = "full-access"


PERMISSION_MODES = {m.value for m in PermissionMode}


VALID_TRANSITIONS: dict[PermissionMode, set[PermissionMode]] = {
    PermissionMode.DEFAULT: {
        PermissionMode.AUTO_REVIEW,
        PermissionMode.FULL_ACCESS,
    },
    PermissionMode.AUTO_REVIEW: {
        PermissionMode.DEFAULT,
        PermissionMode.FULL_ACCESS,
    },
    PermissionMode.FULL_ACCESS: {
        PermissionMode.DEFAULT,
        PermissionMode.AUTO_REVIEW,
    },
}


class InvalidTransitionError(ValueError):
    pass


def normalize_permission_mode(mode: str) -> PermissionMode:
    normalized = mode.strip().lower()
    try:
        return PermissionMode(normalized)
    except ValueError:
        raise ValueError(f"Unknown permission mode: {mode}")


def transition_mode(
    current: PermissionMode,
    target: PermissionMode,
    context: RunContext | None = None,
) -> PermissionMode:
    if target == current:
        return current

    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from '{current.value}' to '{target.value}'"
        )

    return target
