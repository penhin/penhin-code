from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
import pytest

from penhin.agent.context import RunContext
from penhin.cli.commands.plugins import handle_plugin_command
from penhin.plugins.manager import PluginManager
from penhin.plugins.runtime import PluginRegistration, PluginRuntime
from penhin.plugins.service import LocalPluginLoader
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.tools.types import ToolCategory, ToolSpec


def tool(name: str) -> ToolSpec:
    return ToolSpec(name, name, {"type": "object"}, ToolCategory.readonly, lambda: Result.success())


@dataclass
class LoadedPlugin:
    name: str
    closed: bool = False

    def catalog(self) -> ToolCatalog:
        return ToolCatalog([tool(f"{self.name}__lookup")])

    def close(self) -> None:
        self.closed = True


class Loader:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.loaded: list[str] = []
        self.plugin = LoadedPlugin("weather")

    def validate(self, registration: PluginRegistration) -> Result:
        self.validated.append(registration.name)
        return Result.success()

    def load(self, registration: PluginRegistration) -> LoadedPlugin:
        self.loaded.append(registration.name)
        return self.plugin


class PartiallyFailingLoader(Loader):
    def validate(self, registration: PluginRegistration) -> Result:
        if registration.name == "broken":
            return Result.failure("digest mismatch", code="plugin_digest_mismatch")
        return super().validate(registration)


class ReloadLoader:
    def __init__(self) -> None:
        self.loaded: list[LoadedPlugin] = []

    def validate(self, registration: PluginRegistration) -> Result:
        if registration.source == "broken":
            return Result.failure("digest mismatch", code="plugin_digest_mismatch")
        return Result.success(data={"identity": registration.config.get("identity", registration.source), "digest": registration.config.get("digest", registration.source), "capabilities": registration.config.get("capabilities", [])})

    def load(self, registration: PluginRegistration) -> LoadedPlugin:
        plugin = LoadedPlugin(registration.name)
        self.loaded.append(plugin)
        return plugin


class MemoryManager:
    def __init__(self, entries): self.entries = entries
    def effective(self): return self.entries


def write_plugin(root: Path, name: str = "weather") -> None:
    (root / "penhin-plugin.yaml").write_text(
        f"""api_version: 1
name: {name}
tools:
  - name: forecast
    description: Return a forecast.
    entrypoint: plugin:forecast
""",
        encoding="utf-8",
    )
    (root / "plugin.py").write_text("def forecast(): return {'today': 'sunny'}\n", encoding="utf-8")


def test_runtime_discovers_without_starting_hosts_and_activates_namespaced_tools() -> None:
    loader = Loader()
    runtime = PluginRuntime(
        ToolCatalog([tool("read")]),
        [PluginRegistration("weather", "fixtures/weather")],
        loader,
    )

    discovered = runtime.discover()

    assert discovered.ok is True
    assert loader.validated == ["weather"]
    assert loader.loaded == []
    assert runtime.catalog().names() == {"read"}

    activated = runtime.activate("weather")

    assert activated.ok is True
    assert loader.loaded == ["weather"]
    assert runtime.catalog().names() == {"read", "weather__lookup"}


def test_runtime_deactivation_immediately_removes_tools_and_closes_the_host() -> None:
    loader = Loader()
    runtime = PluginRuntime(ToolCatalog([tool("read")]), [PluginRegistration("weather", "fixtures/weather")], loader)
    runtime.discover()
    runtime.activate("weather")

    deactivated = runtime.deactivate("weather")

    assert deactivated.ok is True
    assert loader.plugin.closed is True
    assert runtime.catalog().names() == {"read"}


def test_runtime_isolates_a_failed_plugin_during_discovery() -> None:
    loader = PartiallyFailingLoader()
    runtime = PluginRuntime(
        ToolCatalog([tool("read")]),
        [PluginRegistration("weather", "fixtures/weather"), PluginRegistration("broken", "fixtures/broken")],
        loader,
    )

    discovered = runtime.discover()
    activated = runtime.activate("weather")

    assert discovered.ok is True
    assert activated.ok is True
    assert runtime.catalog().names() == {"read", "weather__lookup"}
    assert runtime.diagnostics() == ({"plugin": "broken", "stage": "discovery", "error": "digest mismatch"},)


def test_runtime_uses_enabled_effective_configuration_only(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project.json")
    manager.install("weather", "global-weather", "global")
    manager.install("weather", "project-weather")
    manager.set_enabled("weather", False)
    manager.install("calendar", "project-calendar")

    runtime = PluginRuntime.from_manager(ToolCatalog([tool("read")]), manager, Loader())

    assert runtime.registrations() == (PluginRegistration("calendar", "project-calendar"),)


def test_local_loader_validates_without_starting_host_then_starts_on_activation(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    runtime = PluginRuntime(ToolCatalog([tool("read")]), [PluginRegistration("weather", str(tmp_path))], LocalPluginLoader())

    assert runtime.discover().ok is True
    assert runtime.catalog().names() == {"read"}
    assert runtime.activate("weather").ok is True
    assert runtime.catalog().names() == {"read", "weather__forecast"}
    runtime.close()


def test_session_commands_change_the_runtime_catalog_without_changing_config() -> None:
    runtime = PluginRuntime(ToolCatalog([tool("read")]), [PluginRegistration("weather", "fixture")], Loader())
    runtime.discover()
    context = RunContext([], PermissionPolicy(set()), ApprovalFlow.require_confirmation(set()), plugin_runtime=runtime)

    with patch("penhin.cli.commands.plugins.ui.print_info"):
        handle_plugin_command(["activate", "weather"], context)
        assert runtime.catalog().names() == {"read", "weather__lookup"}
        assert "weather__lookup" in context.policy.allow
        handle_plugin_command(["deactivate", "weather"], context)

    assert runtime.catalog().names() == {"read"}


def test_discovery_exception_does_not_hide_healthy_plugins() -> None:
    class RaisingLoader(Loader):
        def validate(self, registration):
            if registration.name == "broken":
                raise ValueError("invalid manifest")
            return super().validate(registration)

    runtime = PluginRuntime(ToolCatalog([]), [PluginRegistration("broken", "."), PluginRegistration("weather", ".")], RaisingLoader())
    assert runtime.discover().ok
    assert runtime.activate("weather").ok
    assert runtime.catalog().names() == {"weather__lookup"}
    assert runtime.diagnostics()[0]["plugin"] == "broken"
    runtime.close()


@pytest.mark.parametrize("catalog_raises", [False, True])
def test_activation_failure_closes_host_and_preserves_base_catalog(catalog_raises) -> None:
    loader = Loader()
    base = ToolCatalog([tool("weather__lookup")])
    runtime = PluginRuntime(base, [PluginRegistration("weather", ".")], loader)
    runtime.discover()
    if catalog_raises:
        with patch.object(loader.plugin, "catalog", side_effect=ValueError("bad catalog")):
            result = runtime.activate("weather")
    else:
        result = runtime.activate("weather")
    assert not result.ok
    assert loader.plugin.closed
    assert runtime.active() == ()
    assert runtime.catalog().names() == base.names()


def test_close_continues_after_one_host_fails_and_is_repeatable() -> None:
    plugins = {name: LoadedPlugin(name) for name in ("first", "second")}

    class MultipleLoader(Loader):
        def load(self, registration):
            return plugins[registration.name]

    runtime = PluginRuntime(ToolCatalog([]), [PluginRegistration(name, ".") for name in plugins], MultipleLoader())
    runtime.discover()
    for name in plugins:
        assert runtime.activate(name).ok
    with patch.object(plugins["first"], "close", side_effect=OSError("close failed")):
        runtime.close()
    assert plugins["second"].closed
    assert runtime.catalog().names() == set()
    assert runtime.diagnostics()[0]["stage"] == "cleanup"
    runtime.close()


def test_reload_recomputes_config_preserves_inactive_and_withdraws_removed_plugin(tmp_path: Path) -> None:
    manager = MemoryManager({"weather": {"source": "v1", "config": {"identity": "repo", "digest": "one"}}, "calendar": {"source": "calendar", "config": {}}})
    loader = ReloadLoader()
    runtime = PluginRuntime.from_manager(ToolCatalog([tool("read")]), manager, loader, audit_path=tmp_path / "audit.json")
    runtime.discover()
    runtime.activate("weather")
    turn_catalog = runtime.catalog()
    manager.entries = {"weather": {"source": "v2", "config": {"identity": "repo", "digest": "two"}}, "new": {"source": "new", "config": {"authorized": True}}}

    outcome = runtime.reload()

    assert outcome.ok and runtime.active() == ("weather",)
    assert runtime.available() == ("new", "weather")
    assert turn_catalog.names() == {"read", "weather__lookup"}  # acquired snapshot is stable
    assert runtime.audit_records()[0]["plugins"][-1]["result_code"] == "discovered"
    assert "credential" not in (tmp_path / "audit.json").read_text(encoding="utf-8")


def test_reload_authorization_gate_keeps_active_artifact_and_isolates_failure() -> None:
    manager = MemoryManager({"weather": {"source": "old", "config": {"identity": "one", "capabilities": ["state"]}}, "healthy": {"source": "healthy", "config": {}}})
    runtime = PluginRuntime.from_manager(ToolCatalog([]), manager, ReloadLoader())
    runtime.discover(); runtime.activate("weather")
    original = runtime.catalog()
    manager.entries = {"weather": {"source": "new", "config": {"identity": "other", "capabilities": ["state", "network"]}}, "healthy": {"source": "healthy", "config": {}}, "broken": {"source": "broken", "config": {}}}

    result = runtime.reload()

    records = {item["plugin"]: item for item in result.data["plugins"]}
    assert runtime.active() == ("weather",)
    assert runtime.catalog().names() == original.names()
    assert records["weather"]["result_code"] == "authorization_pending"
    assert records["broken"]["result_code"] == "plugin_digest_mismatch"


def test_reload_publishes_a_new_complete_generation_only_after_replacement() -> None:
    manager = MemoryManager({"weather": {"source": "one", "config": {"identity": "repo", "authorized": True}}})
    runtime = PluginRuntime.from_manager(ToolCatalog([]), manager, ReloadLoader())
    runtime.discover(); runtime.activate("weather")
    first = runtime.generation("weather")
    manager.entries["weather"] = {"source": "two", "config": {"identity": "repo", "authorized": True}}

    runtime.reload()

    second = runtime.generation("weather")
    assert first is not None and second is not None
    assert first.number + 1 == second.number
    assert first.catalog.names() == second.catalog.names() == {"weather__lookup"}


def test_reload_command_delegates_to_runtime() -> None:
    runtime = PluginRuntime.from_manager(ToolCatalog([]), MemoryManager({}), ReloadLoader())
    context = RunContext([], PermissionPolicy(set()), ApprovalFlow.require_confirmation(set()), plugin_runtime=runtime)
    with patch("penhin.cli.commands.plugins.ui.print_json") as printed:
        handle_plugin_command(["reload"], context)
    assert printed.called
