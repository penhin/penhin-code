import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from config import ENV_FILE
from providers.anthropic import AnthropicProvider
from providers.types import LLMProvider, LLMRequest, LLMResponse, StreamCallback


runtime = None

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
            try:
                request = LLMRequest(
                    model=self.model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens or self.max_tokens,
                )
                if stream_callback is None:
                    response = self.provider.create_message(request)
                else:
                    response = self.provider.stream_message(request, stream_callback)
                if breaker is not None:
                    breaker.record_success()
                return response
            except retry_errors as error:
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
                time.sleep(delay)

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

    def call_llm_once(
        self,
        system: str,
        user_content: str,
        max_tokens: int | None = None,
        label: str = "llm",
    ) -> str:
        response = self.call_with_retry(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens or self.max_tokens,
        )

        log_usage(label, response)

        return "\n".join(
            block.get("text", "")
            for block in response.content
            if block.get("type") == "text"
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
    load_dotenv(ENV_FILE, override=False)
    load_dotenv(".env", override=False)

    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}.get(provider)
    if key_name is None:
        logger.error(f"Unsupported LLM_PROVIDER={provider!r}; choose anthropic, openai, or gemini")
        raise SystemExit(1)
    missing_env = [name for name in (key_name, "MODEL_ID") if not os.getenv(name)]
    if missing_env:
        logger.error(f"Please configure {', '.join(missing_env)} in {ENV_FILE} or .env")
        sys.exit(1)

    model = os.environ["MODEL_ID"]
    
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
    if runtime is not None:
        runtime.model = model


def set_runtime_api_key(api_key: str) -> None:
    if runtime is not None:
        runtime.provider = build_provider_from_env()


def build_provider_from_env() -> LLMProvider:
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicProvider.from_env()
    if provider == "openai":
        from providers.openai import OpenAIProvider
        return OpenAIProvider.from_env()
    if provider == "gemini":
        from providers.gemini import GeminiProvider
        return GeminiProvider.from_env()
    raise ValueError(f"Unsupported LLM_PROVIDER={provider!r}")
