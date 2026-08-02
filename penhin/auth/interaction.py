from __future__ import annotations

from typing import Protocol


class AuthInteraction(Protocol):
    def prompt(self, kind: str, message: str, options: tuple[tuple[str, str], ...] = ()) -> str: ...
    def notify(self, kind: str, **payload: object) -> None: ...
