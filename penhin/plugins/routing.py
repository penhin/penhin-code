from dataclasses import dataclass

from penhin.tools.catalog import ToolCatalog


@dataclass(frozen=True)
class PluginCapability:
    plugin: str
    labels: tuple[str, ...]
    triggers: tuple[str, ...]
    catalog: ToolCatalog


@dataclass(frozen=True)
class Route:
    catalog: ToolCatalog | None
    selected: str | None
    fallback: bool


class PluginRouter:
    def __init__(self, capabilities: list[PluginCapability]) -> None:
        self.capabilities = capabilities

    def route(self, request: str) -> Route:
        text = request.lower()
        matches = [item for item in self.capabilities if any(term.lower() in text for term in (*item.labels, *item.triggers))]
        if len(matches) != 1:
            return Route(None, None, True)
        selected = matches[0]
        return Route(selected.catalog, selected.plugin, False)
