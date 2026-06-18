import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger("penhin.circuit")


class CircuitBreakerOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    success_threshold: int = 1
    clock: Callable[[], float] = time.monotonic
    state: str = field(default="closed", init=False)
    failure_count: int = field(default=0, init=False)
    success_count: int = field(default=0, init=False)
    opened_at: float | None = field(default=None, init=False)

    def before_call(self) -> None:
        if self.state != "open":
            return

        elapsed = self.clock() - (self.opened_at or 0.0)
        if elapsed >= self.recovery_timeout:
            self.state = "half_open"
            self.success_count = 0
            logger.warning("circuit breaker half_open (trial attempt)")
            return

        retry_after = max(0.0, self.recovery_timeout - elapsed)
        raise CircuitBreakerOpen(
            f"Circuit breaker is open; retry after {retry_after:.0f}s"
        )

    def record_success(self) -> None:
        if self.state == "half_open":
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self._close()
            return

        if self.state == "closed":
            self.failure_count = 0

    def record_failure(self) -> None:
        if self.state == "half_open":
            self._open()
            return

        if self.state != "closed":
            return

        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.state = "open"
        self.opened_at = self.clock()
        self.success_count = 0
        logger.warning(
            "circuit breaker opened "
            f"(failure_count={self.failure_count}/{self.failure_threshold})"
        )

    def _close(self) -> None:
        self.state = "closed"
        self.failure_count = 0
        self.success_count = 0
        self.opened_at = None
        logger.warning("circuit breaker closed (recovered)")

    def snapshot(self) -> dict[str, Any]:
        retry_after = None
        if self.state == "open" and self.opened_at is not None:
            retry_after = max(0.0, self.recovery_timeout - (self.clock() - self.opened_at))

        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "success_count": self.success_count,
            "success_threshold": self.success_threshold,
            "recovery_timeout": self.recovery_timeout,
            "opened_at": self.opened_at,
            "retry_after": retry_after,
        }
