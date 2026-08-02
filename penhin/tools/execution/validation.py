from __future__ import annotations

from penhin.result import Result
from penhin.tools.registry import TOOL_SPECS
from penhin.tools.types import ToolInput


TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def unknown_tool_input_fields(tool_name: str, tool_input: ToolInput) -> list[str]:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return []
    return sorted(set(tool_input) - set(spec.input_schema.get("properties", {})))


def validate_tool_input(tool_name: str, tool_input: ToolInput) -> Result | None:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return Result.failure(f"Unknown tool: {tool_name}", code="unknown_tool")
    properties = spec.input_schema.get("properties", {})
    missing = [name for name in spec.input_schema.get("required", []) if name not in tool_input]
    if missing:
        return Result.failure(f"Missing required input: {', '.join(missing)}", code="invalid_tool_input", missing=missing)
    for name, value in tool_input.items():
        field_schema = properties.get(name)
        if field_schema is None:
            continue
        expected = field_schema.get("type")
        checker = TYPE_CHECKS.get(expected)
        if checker is not None and not checker(value):
            return Result.failure(
                f"Invalid input type: {name} expected {expected}", code="invalid_tool_input",
                field=name, expected=expected, actual=type(value).__name__,
            )
        allowed = field_schema.get("enum")
        if allowed is not None and value not in allowed:
            return Result.failure(
                f"Invalid input value: {name} must be one of {', '.join(map(str, allowed))}",
                code="invalid_tool_input", field=name, expected="enum", allowed=allowed, actual=value,
            )
    return None


__all__ = ["unknown_tool_input_fields", "validate_tool_input"]
