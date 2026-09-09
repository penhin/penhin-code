"""Thread-safe queue for prompts submitted while the agent is busy."""
from __future__ import annotations

from collections import deque
from threading import Condition


class PromptQueue:
    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._condition = Condition()
        self._closed = False

    def submit(self, prompt: str) -> None:
        with self._condition:
            self._items.append(prompt)
            self._condition.notify()

    def next(self) -> str | None:
        with self._condition:
            self._condition.wait_for(lambda: self._items or self._closed)
            return self._items.popleft() if self._items else None

    def restore_all(self) -> list[str]:
        with self._condition:
            prompts = list(self._items)
            self._items.clear()
            return prompts

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def size(self) -> int:
        with self._condition:
            return len(self._items)
