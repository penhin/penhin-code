# PluginRuntime uses lazy hosts and immediate catalog removal

PluginRuntime discovers and validates every effective Plugin Artifact at run start, but starts a Plugin host only when that Plugin is explicitly activated. Deactivation immediately removes its Plugin Contributions from the current catalog so later model turns cannot call them; this preserves a stable, auditable activation decision without paying host startup cost for inactive Plugins.
