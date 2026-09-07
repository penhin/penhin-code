from penhin.plugins.routing import PluginCapability, PluginRouter
from penhin.tools.catalog import ToolCatalog
from penhin.tools.types import ToolCategory, ToolSpec
from penhin.result import Result


def catalog(name):
    return ToolCatalog([ToolSpec(name, name, {"type":"object","properties":{},"required":[]}, ToolCategory.readonly, lambda: Result.success("ok"))])


def test_router_loads_only_the_deterministic_matching_plugin() -> None:
    weather, calendar = catalog("weather__lookup"), catalog("calendar__lookup")
    router = PluginRouter([PluginCapability("weather", ("weather",), ("forecast",), weather), PluginCapability("calendar", ("calendar",), ("schedule",), calendar)])
    route = router.route("What is the weather forecast?")
    assert route.selected == "weather" and not route.fallback
    assert [item["name"] for item in route.catalog.schemas()] == ["weather__lookup"]


def test_router_uses_fallback_for_irrelevant_or_ambiguous_requests() -> None:
    router = PluginRouter([PluginCapability("weather", ("weather",), (), catalog("weather__lookup")), PluginCapability("calendar", ("calendar",), (), catalog("calendar__lookup"))])
    assert router.route("hello").fallback
    assert router.route("weather and calendar").fallback
