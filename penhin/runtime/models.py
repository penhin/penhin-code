from __future__ import annotations

from dataclasses import dataclass


class AuthenticationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeStatus:
    available: bool
    provider: str
    model: str
    auth_expires_at: int | None = None
