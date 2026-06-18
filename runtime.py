import os
import sys
import time
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIError, RateLimitError
from dotenv import load_dotenv

from circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from config import ENV_FILE


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
    client: Anthropic
    model: str
    max_tokens: int = 10000
    sub_max_turns: int = 30
    sub_max_tokens: int = 2000
    retry_delays: tuple[int, ...] = (1, 2, 4)
    circuit_breaker: CircuitBreaker | None = None

    def call_with_retry(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, max_tokens: int | None = None):
        retry_errors = (APIError, APIConnectionError, RateLimitError)
        delays = self.retry_delays

        if self.circuit_breaker is not None:
            try:
                self.circuit_breaker.before_call()
            except CircuitBreakerOpen as error:
                logger.warning(f"[circuit] messages.create skipped: {error}")
                raise

        for attempt in range(len(delays) + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "system": system,
                    "messages": messages,
                    "max_tokens": max_tokens or self.max_tokens,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                response = self.client.messages.create(**kwargs)
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                return response
            except retry_errors as error:
                if attempt == len(delays):
                    if self.circuit_breaker is not None:
                        self.circuit_breaker.record_failure()
                    raise

                delay = delays[attempt]
                logger.warning(
                    f"[retry] messages.create failed ({error.__class__.__name__}), "
                    f"retrying in {delay}s...\n"
                    f"[retry] Reconnecting...({attempt + 1}/{len(delays)})"
                )
                time.sleep(delay)

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
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
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


def init_runtime() -> None:
    setup_logging()

    global runtime
    load_dotenv(ENV_FILE, override=False)
    load_dotenv(".env", override=False)

    missing_env = [name for name in ("ANTHROPIC_API_KEY", "MODEL_ID") if not os.getenv(name)]
    if missing_env:
        logger.error(f"Please configure {', '.join(missing_env)} in {ENV_FILE} or .env")
        sys.exit(1)

    client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    model = os.environ["MODEL_ID"]
    
    runtime = Runtime(
        client=client,
        model=model,
        circuit_breaker=build_circuit_breaker_from_env(),
    )

def get_runtime() -> Runtime:
    if runtime is None:
        raise RuntimeError("init_runtime() must be called before get_runtime()")
    return runtime
