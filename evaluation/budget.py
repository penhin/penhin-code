from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000


@dataclass
class Budget:
    max_tokens: int
    max_usd: float
    used_input_tokens: int = 0
    used_output_tokens: int = 0
    used_usd: float = 0.0

    @property
    def used_tokens(self) -> int:
        return self.used_input_tokens + self.used_output_tokens

    def reserve(self, estimated_input: int, max_output: int, price: ModelPrice) -> None:
        projected_tokens = self.used_tokens + estimated_input + max_output
        projected_usd = self.used_usd + price.cost(estimated_input, max_output)
        if projected_tokens > self.max_tokens:
            raise BudgetExceeded(f"token budget would be exceeded: {projected_tokens}>{self.max_tokens}")
        if projected_usd > self.max_usd:
            raise BudgetExceeded(f"USD budget would be exceeded: {projected_usd:.6f}>{self.max_usd:.6f}")

    def record(self, input_tokens: int, output_tokens: int, price: ModelPrice) -> None:
        self.used_input_tokens += input_tokens
        self.used_output_tokens += output_tokens
        self.used_usd += price.cost(input_tokens, output_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
            "used_input_tokens": self.used_input_tokens,
            "used_output_tokens": self.used_output_tokens,
            "used_tokens": self.used_tokens,
            "used_usd": round(self.used_usd, 8),
        }
