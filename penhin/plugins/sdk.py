"""Small authoring helpers for the initial read-only Python plugin contract."""

from __future__ import annotations

from typing import Any, Callable


def tool(description: str, input_schema: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Annotate a callable declared by a plugin manifest.

    The host intentionally invokes only the manifest entrypoint in this beta; the
    annotation lets authors keep the schema and friendly description beside code
    and is checked by the loader in the next publishing phase.
    """
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        function.__penhin_tool__ = {"description": description, "input_schema": input_schema}
        return function
    return decorate
