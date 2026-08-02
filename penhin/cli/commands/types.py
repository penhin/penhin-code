from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from penhin.agent.context import RunContext


CommandHandler = Callable[[list[str], RunContext | None], None]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: CommandHandler
