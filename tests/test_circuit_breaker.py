import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.runtime import manager as runtime
from penhin.runtime import settings as runtime_settings
from penhin.runtime.retry import CircuitBreaker, CircuitBreakerOpen


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    retry_errors = (RuntimeError,)

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.create_calls = 0
        self.stream_calls = 0

    def create_message(self, request):
        self.create_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def stream_message(self, request, stream_callback):
        self.stream_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for chunk in getattr(outcome, "chunks", []):
            stream_callback(chunk)
        return outcome.final_message


class FakeStreamOutcome:
    def __init__(self, chunks, final_message) -> None:
        self.chunks = chunks
        self.final_message = final_message


def test_circuit_breaker_opens_after_threshold() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10, clock=clock)

    breaker.record_failure()
    assert breaker.state == "closed"

    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.opened_at == 0.0


def test_circuit_breaker_rejects_while_open() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    breaker.record_failure()
    clock.advance(3)

    try:
        breaker.before_call()
    except CircuitBreakerOpen as error:
        assert "retry after 7s" in str(error)
    else:
        raise AssertionError("expected CircuitBreakerOpen")


def test_circuit_breaker_half_open_success_closes() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    breaker.record_failure()
    clock.advance(10)

    breaker.before_call()
    assert breaker.state == "half_open"

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failure_count == 0


def test_circuit_breaker_half_open_failure_reopens() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    breaker.record_failure()
    clock.advance(10)
    breaker.before_call()

    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.opened_at == 10.0


def test_circuit_breaker_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10)

    breaker.record_failure()
    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.failure_count == 0


def test_circuit_breaker_snapshot_includes_retry_after() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    breaker.record_failure()
    clock.advance(4)

    snapshot = breaker.snapshot()

    assert snapshot["state"] == "open"
    assert snapshot["retry_after"] == 6


def test_runtime_counts_only_final_retry_failure() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
    provider = FakeProvider([RuntimeError("first"), RuntimeError("second")])
    llm_runtime = runtime.Runtime(
        provider=provider,
        model="test-model",
        retry_delays=(0,),
        circuit_breaker=breaker,
    )

    try:
        llm_runtime.call_with_retry(system="s", messages=[])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")

    assert provider.create_calls == 2
    assert breaker.failure_count == 1
    assert breaker.state == "closed"


def test_runtime_open_circuit_skips_client_call() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10)
    breaker.record_failure()
    provider = FakeProvider(["ok"])
    llm_runtime = runtime.Runtime(
        provider=provider,
        model="test-model",
        retry_delays=(),
        circuit_breaker=breaker,
    )

    try:
        llm_runtime.call_with_retry(system="s", messages=[])
    except CircuitBreakerOpen:
        pass
    else:
        raise AssertionError("expected CircuitBreakerOpen")

    assert provider.create_calls == 0


def test_runtime_compact_breaker_is_independent_from_main_breaker() -> None:
    main_breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
    compact_breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10)
    provider = FakeProvider([RuntimeError("compact failed")])
    llm_runtime = runtime.Runtime(
        provider=provider,
        model="test-model",
        retry_delays=(),
        circuit_breaker=main_breaker,
        compact_circuit_breaker=compact_breaker,
    )

    try:
        llm_runtime.call_compact_once(system="s", user_content="u")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")

    assert main_breaker.failure_count == 0
    assert main_breaker.state == "closed"
    assert compact_breaker.failure_count == 1
    assert compact_breaker.state == "open"


def test_runtime_streams_text_deltas_and_returns_final_message() -> None:
    final_message = object()
    provider = FakeProvider([FakeStreamOutcome(["hel", "lo"], final_message)])
    llm_runtime = runtime.Runtime(
        provider=provider,
        model="test-model",
        retry_delays=(),
    )
    chunks = []

    response = llm_runtime.call_with_retry(
        system="s",
        messages=[],
        stream_callback=chunks.append,
    )

    assert response is final_message
    assert chunks == ["hel", "lo"]
    assert provider.stream_calls == 1


def test_build_circuit_breaker_from_env_can_disable() -> None:
    with patch.dict("os.environ", {"CIRCUIT_BREAKER_ENABLED": "false"}):
        assert runtime_settings.build_circuit_breaker() is None


def test_build_circuit_breaker_from_env_uses_config_values() -> None:
    with patch.dict(
        "os.environ",
        {
            "CIRCUIT_BREAKER_ENABLED": "true",
            "CIRCUIT_BREAKER_FAILURE_THRESHOLD": "3",
            "CIRCUIT_BREAKER_RECOVERY_TIMEOUT": "12.5",
            "CIRCUIT_BREAKER_SUCCESS_THRESHOLD": "2",
        },
    ):
        breaker = runtime_settings.build_circuit_breaker()

    assert breaker is not None
    assert breaker.failure_threshold == 3
    assert breaker.recovery_timeout == 12.5
    assert breaker.success_threshold == 2


def test_build_compact_circuit_breaker_from_env_uses_config_values() -> None:
    with patch.dict(
        "os.environ",
        {
            "COMPACT_CIRCUIT_BREAKER_ENABLED": "true",
            "COMPACT_CIRCUIT_BREAKER_FAILURE_THRESHOLD": "4",
            "COMPACT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT": "90",
            "COMPACT_CIRCUIT_BREAKER_SUCCESS_THRESHOLD": "2",
        },
    ):
        breaker = runtime_settings.build_compact_circuit_breaker()

    assert breaker is not None
    assert breaker.failure_threshold == 4
    assert breaker.recovery_timeout == 90
    assert breaker.success_threshold == 2
