"""Composition helpers for a run-scoped governed PluginRuntime."""

from __future__ import annotations

from pathlib import Path

from penhin.plugins.manager import PluginManager
from penhin.plugins.installation import OFFICIAL_PUBLISHER_TRUST_ROOTS
from penhin.plugins.runtime import PluginRuntime
from penhin.plugins.service import LocalPluginLoader
from penhin.tools.catalog import ToolCatalog
from penhin.tools.registry import DEFAULT_TOOL_CATALOG


def plugin_runtime_for_session(
    base_catalog: ToolCatalog = DEFAULT_TOOL_CATALOG,
    cwd: Path | None = None,
) -> PluginRuntime:
    """Discover the effective Plugins for a run without activating hosts."""
    project_root = cwd or Path.cwd()
    manager = PluginManager(Path.home() / ".penhin/plugins.json", project_root / ".penhin/plugins.json")
    runtime = PluginRuntime.from_manager(
        base_catalog, manager, LocalPluginLoader({**OFFICIAL_PUBLISHER_TRUST_ROOTS, **manager.project_trust_roots()}), audit_path=project_root / ".penhin/plugin-reload-audit.json"
    )
    runtime.discover()
    return runtime
