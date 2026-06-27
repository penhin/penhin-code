from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


ContentBlock = dict[str, Any]
StreamCallback = Callable[[str], None]


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]
    max_tokens: int
    tools: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: list[ContentBlock]
    stop_reason: str
    usage: LLMUsage


class LLMProvider(Protocol):
    retry_errors: tuple[type[BaseException], ...]

    def create_message(self, request: LLMRequest) -> LLMResponse:
        ...

    def stream_message(
        self,
        request: LLMRequest,
        stream_callback: StreamCallback,
    ) -> LLMResponse:
        ...
