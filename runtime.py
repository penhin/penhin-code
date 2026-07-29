import logging
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from config import ENV_FILE
from providers.anthropic import AnthropicProvider
from providers.models import validate_model
from providers.types import LLMProvider, LLMRequest, LLMResponse, StreamCallback


runtime = None
environment_sources: dict[str, str] = {}

logger = logging.getLogger("penhin.runtime")


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{self.RESET}"


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter("%(message)s"))
    
    app_logger = logging.getLogger("penhin")
    app_logger.setLevel(logging.INFO)
    app_logger.handlers.clear()
    app_logger.addHandler(handler)


@dataclass
class Runtime:
    provider: LLMProvider
    model: str
    max_tokens: int = 10000
    sub_max_turns: int = 30
    sub_max_tokens: int = 2000
    retry_delays: tuple[int, ...] = (1, 2, 4)
    circuit_breaker: CircuitBreaker | None = None
    compact_circuit_breaker: CircuitBreaker | None = None

    def _call_with_retry(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        breaker: CircuitBreaker | None = None,
        label: str = "messages.create",
        stream_callback: StreamCallback | None = None,
    ) -> LLMResponse:
        retry_errors = self.provider.retry_errors
        delays = self.retry_delays

        if breaker is not None:
            try:
                breaker.before_call()
            except CircuitBreakerOpen as error:
                logger.warning(f"[circuit] {label} skipped: {error}")
                raise

        for attempt in range(len(delays) + 1):
            started = time.perf_counter()
            first_token_ms: float | None = None
            reservation_id = None
            budget = None
            try:
                request = LLMRequest(
                    model=self.model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens or self.max_tokens,
                )
                from evaluation.observer import anonymous_id, emit
                from evaluation.shared_budget import budget_from_env, estimate_tokens, price_from_env
                budget = budget_from_env()
                if budget is not None:
                    reservation_id = budget.reserve(
                        estimate_tokens({"system": system, "messages": messages, "tools": tools}),
                        request.max_tokens,
                        price_from_env("primary"),
                        "primary",
                        os.getenv("PENHIN_EVAL_BUDGET_CASE_KEY", os.getenv("PENHIN_EVAL_CASE_ID", "")),
                        int(os.getenv("PENHIN_EVAL_CURRENT_CASE_MAX_TOKENS", "0")) or None,
                    )
                request_digest = hashlib.sha256(json.dumps(
                    {"system": system, "messages": messages, "tools": tools},
                    ensure_ascii=False, sort_keys=True, default=str,
                ).encode("utf-8")).hexdigest()[:16]
                emit("llm_call_started", label=label, provider=configured_provider(), model_id_hash=anonymous_id(self.model), attempt=attempt + 1, max_tokens=request.max_tokens, request_digest=request_digest)
                if stream_callback is None:
                    response = self.provider.create_message(request)
                else:
                    def observed_stream(text: str) -> None:
                        nonlocal first_token_ms
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        stream_callback(text)
                    response = self.provider.stream_message(request, observed_stream)
                if budget is not None and reservation_id is not None:
                    budget.settle(reservation_id, response.usage.input_tokens, response.usage.output_tokens, price_from_env("primary"))
                    reservation_id = None
                emit(
                    "llm_call_completed", label=label, provider=configured_provider(), model_id_hash=anonymous_id(self.model),
                    attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000,
                    first_token_ms=first_token_ms, stop_reason=getattr(response, "stop_reason", ""),
                    usage=getattr(response, "usage", None),
                )
                if breaker is not None:
                    breaker.record_success()
                return response
            except retry_errors as error:
                if budget is not None and reservation_id is not None:
                    budget.release(reservation_id)
                from evaluation.observer import emit
                emit("llm_call_failed", label=label, attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000, error_type=type(error).__name__, retryable=True)
                if attempt == len(delays):
                    if breaker is not None:
                        breaker.record_failure()
                    raise

                delay = delays[attempt]
                logger.warning(
                    f"[retry] {label} failed ({error.__class__.__name__}), "
                    f"retrying in {delay}s...\n"
                    f"[retry] Reconnecting...({attempt + 1}/{len(delays)})"
                )
                emit("llm_retry", label=label, attempt=attempt + 1, delay_seconds=delay, error_type=type(error).__name__)
                time.sleep(delay)
            except Exception as error:
                if budget is not None and reservation_id is not None:
                    budget.release(reservation_id)
                from evaluation.observer import emit
                emit("llm_call_failed", label=label, attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000, error_type=type(error).__name__, retryable=False)
                raise

    def call_with_retry(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        stream_callback: StreamCallback | None = None,
    ) -> LLMResponse:
        return self._call_with_retry(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            breaker=self.circuit_breaker,
            label="messages.create",
            stream_callback=stream_callback,
        )

    def call_compact_once(
        self,
        system: str,
        user_content: str,
        max_tokens: int | None = None,
    ) -> str:
        response = self._call_with_retry(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens or self.max_tokens,
            breaker=self.compact_circuit_breaker,
            label="compact.messages.create",
        )

        log_usage("compact", response)

        return "\n".join(
            block.get("text", "")
            for block in response.content
            if block.get("type") == "text"
        )


def log_usage(label: str, response) -> None:
    usage = response.usage
    logger.info(
        f"[usage:{label}] "
        f"input={usage.input_tokens} "
        f"output={usage.output_tokens} "
        f"total={usage.input_tokens + usage.output_tokens} "
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"[config] invalid {name}={value!r}; using {default}")
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[config] invalid {name}={value!r}; using {default}")
        return default


def build_circuit_breaker_from_env() -> CircuitBreaker | None:
    if not _env_bool("CIRCUIT_BREAKER_ENABLED", True):
        return None

    return CircuitBreaker(
        failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
        recovery_timeout=_env_float("CIRCUIT_BREAKER_RECOVERY_TIMEOUT", 30.0),
        success_threshold=_env_int("CIRCUIT_BREAKER_SUCCESS_THRESHOLD", 1),
    )


def build_compact_circuit_breaker_from_env() -> CircuitBreaker | None:
    if not _env_bool("COMPACT_CIRCUIT_BREAKER_ENABLED", True):
        return None

    return CircuitBreaker(
        failure_threshold=_env_int("COMPACT_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2),
        recovery_timeout=_env_float("COMPACT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT", 60.0),
        success_threshold=_env_int("COMPACT_CIRCUIT_BREAKER_SUCCESS_THRESHOLD", 1),
    )


def init_runtime() -> None:
    setup_logging()

    global runtime
    load_runtime_environment()

    provider = configured_provider()
    key_name = provider_key_name(provider)
    if key_name is None:
        logger.error(f"Unsupported LLM_PROVIDER={provider!r}; choose anthropic, openai, or gemini")
        raise SystemExit(1)
    missing_env = [name for name in (key_name, "MODEL_ID") if not os.getenv(name)]
    if missing_env:
        logger.error(f"Please configure {', '.join(missing_env)} in {ENV_FILE} or .env")
        sys.exit(1)

    model = os.environ["MODEL_ID"]
    try:
        validate_model(provider, model)
    except ValueError as error:
        logger.error(str(error))
        raise SystemExit(1)
    
    runtime = Runtime(
        provider=build_provider_from_env(),
        model=model,
        circuit_breaker=build_circuit_breaker_from_env(),
        compact_circuit_breaker=build_compact_circuit_breaker_from_env(),
    )


def get_runtime() -> Runtime:
    if runtime is None:
        raise RuntimeError("init_runtime() must be called before get_runtime()")
    return runtime


def set_runtime_model(model: str) -> None:
    validate_model(configured_provider(), model)
    if runtime is not None:
        runtime.model = model


def set_runtime_api_key(api_key: str) -> None:
    if runtime is not None:
        runtime.provider = build_provider_from_env()


def set_runtime_provider(provider: str, model: str) -> None:
    if provider_key_name(provider) is None:
        raise ValueError(f"Unsupported provider: {provider}")
    validate_model(provider, model)
    if runtime is not None:
        runtime.provider = build_provider_from_env()
        runtime.model = model


def provider_key_name(provider: str) -> str | None:
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }.get(provider)


def configured_provider() -> str:
    return os.getenv("LLM_PROVIDER", "").strip().lower() or "anthropic"


def setting_source(name: str) -> str:
    if not os.getenv(name):
        return "not set"
    return environment_sources.get(name, "Process environment")


def mark_setting_source(name: str, source: str) -> None:
    environment_sources[name] = source


def load_runtime_environment() -> None:
    environment_sources.clear()
    original_names = set(os.environ)
    for path, label in ((ENV_FILE, "User env"), (Path(".env"), "Project env")):
        values = dotenv_values(path)
        load_dotenv(path, override=False)
        for name, value in values.items():
            if name not in original_names and value is not None and name not in environment_sources:
                environment_sources[name] = label
    for name in original_names:
        environment_sources[name] = "Process environment"


def build_provider_from_env() -> LLMProvider:
    provider = configured_provider()
    if provider == "anthropic":
        return AnthropicProvider.from_env()
    if provider == "openai":
        from providers.openai import OpenAIProvider
        return OpenAIProvider.from_env()
    if provider == "gemini":
        from providers.gemini import GeminiProvider
        return GeminiProvider.from_env()
    raise ValueError(f"Unsupported LLM_PROVIDER={provider!r}")
