"""Immutable tool catalogs used to isolate a runtime's available tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from penhin.tools.types import ToolSchema, ToolSpec, tool_schema


class ToolCatalog:
    """A stable, injectable view of the tools available to one runtime.

    The catalog owns a snapshot of its specs so registering a future plugin cannot
    mutate another running agent's tool surface.
    """

    def __init__(self, specs: Mapping[str, ToolSpec] | Iterable[ToolSpec]) -> None:
        items = specs.items() if isinstance(specs, Mapping) else ((spec.name, spec) for spec in specs)
        catalog: dict[str, ToolSpec] = {}
        for name, spec in items:
            if name != spec.name:
                raise ValueError(f"Tool catalog key {name!r} does not match spec name {spec.name!r}")
            if name in catalog:
                raise ValueError(f"Duplicate tool name: {name}")
            catalog[name] = spec
        self._specs = MappingProxyType(catalog)

    @property
    def specs(self) -> Mapping[str, ToolSpec]:
        return self._specs

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def schemas(self, scope: str = "parent") -> list[ToolSchema]:
        if scope not in {"parent", "child"}:
            raise ValueError(f"Unknown tool scope: {scope}")
        attribute = "available_to_parent" if scope == "parent" else "available_to_child"
        return [tool_schema(spec) for spec in self._specs.values() if getattr(spec, attribute)]

    def names(self, scope: str | None = None) -> set[str]:
        if scope is None:
            return set(self._specs)
        if scope not in {"parent", "child"}:
            raise ValueError(f"Unknown tool scope: {scope}")
        attribute = "available_to_parent" if scope == "parent" else "available_to_child"
        return {name for name, spec in self._specs.items() if getattr(spec, attribute)}

    def description_lines(self, scope: str = "parent") -> list[str]:
        return [f"- {tool['name']}: {tool['description']}" for tool in self.schemas(scope)]

    def merged(self, *catalogs: ToolCatalog) -> ToolCatalog:
        """Return a new catalog without mutating any running catalog."""
        specs = dict(self._specs)
        for catalog in catalogs:
            duplicates = set(specs) & set(catalog.specs)
            if duplicates:
                raise ValueError(f"Duplicate tool names: {', '.join(sorted(duplicates))}")
            specs.update(catalog.specs)
        return ToolCatalog(specs)
