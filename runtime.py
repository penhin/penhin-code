import os
import sys
import time
import logging

from typing import Any
from dotenv import load_dotenv
from dataclasses import dataclass
from anthropic import Anthropic, APIConnectionError, APIError, RateLimitError


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

    def call_with_retry(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, max_tokens: int | None = None):
        retry_errors = (APIError, APIConnectionError, RateLimitError)
        delays = self.retry_delays

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
                return self.client.messages.create(**kwargs)
            except retry_errors as error:
                if attempt == len(delays):
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


def init_runtime() -> None:
    setup_logging()

    global runtime
    load_dotenv()

    missing_env = [name for name in ("ANTHROPIC_API_KEY", "MODEL_ID") if not os.getenv(name)]
    if missing_env:
        logger.error(f"Please configure {', '.join(missing_env)} in .env")
        sys.exit(1)

    client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    model = os.environ["MODEL_ID"]
    
    runtime = Runtime(
        client=client,
        model=model
    )

def get_runtime() -> Runtime:
    if runtime is None:
        raise RuntimeError("init_runtime() must be called before get_runtime()")
    return runtime
