import os
import sys

from dataclasses import dataclass
from anthropic import Anthropic
from dotenv import load_dotenv


runtime = None


@dataclass
class Runtime:
    client: Anthropic
    model: str
    max_tokens: int = 10000
    sub_max_turns: int = 30
    sub_max_tokens: int = 2000
    

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

def call_llm_once(
    system: str,
    user_content: str,
    max_tokens: int | None = None,
    label: str = "llm",
) -> str:
    runtime = get_runtime()

    response = runtime.client.messages.create(
        model=runtime.model,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=max_tokens or runtime.max_tokens,
    )

    print_usage(label, response)

    return "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )
