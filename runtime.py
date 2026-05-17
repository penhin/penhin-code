import os
import sys
import time

from dataclasses import dataclass
from anthropic import Anthropic, APIConnectionError, APIError, RateLimitError
from dotenv import load_dotenv


runtime = None


@dataclass
class Runtime:
    client: Anthropic
    model: str
    max_tokens: int = 10000
    sub_max_turns: int = 30
    sub_max_tokens: int = 2000

    def call_with_retry(self, system: str, messages: list[dict], tools=None, max_tokens: int | None = None):
        retry_errors = (APIError, APIConnectionError, RateLimitError)
        delays = [1, 2, 4]

        for attempt in range(len(delays) + 1):
            try:
                kwargs = {
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
                print(
                    f"[retry] messages.create failed ({error.__class__.__name__}), "
                    f"retrying in {delay}s..."
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

        print_usage(label, response)

        return "\n".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )

def print_usage(label: str, response) -> None:
    usage = response.usage
    print(
        f"[usage:{label}] "
        f"input={usage.input_tokens} "
        f"output={usage.output_tokens} "
        f"total={usage.input_tokens + usage.output_tokens} "
    )


def init_runtime() -> None:
    global runtime
    load_dotenv()

    missing_env = [name for name in ("ANTHROPIC_API_KEY", "MODEL_ID") if not os.getenv(name)]
    if missing_env:
        print(f"Please configure {', '.join(missing_env)} in .env")
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
