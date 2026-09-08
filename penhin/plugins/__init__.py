"""Governed plugin loading primitives."""

__all__ = ["LocalPlugin", "LocalPluginLoader", "PluginError", "PluginGeneration", "PluginRegistration", "PluginRuntime", "load_local_plugin"]


def __getattr__(name: str):
    if name in {"PluginGeneration", "PluginRegistration", "PluginRuntime"}:
        from .runtime import PluginGeneration, PluginRegistration, PluginRuntime
        return {"PluginGeneration": PluginGeneration, "PluginRegistration": PluginRegistration, "PluginRuntime": PluginRuntime}[name]
    if name in __all__:
        from .service import LocalPlugin, LocalPluginLoader, PluginError, load_local_plugin
        return {"LocalPlugin": LocalPlugin, "LocalPluginLoader": LocalPluginLoader, "PluginError": PluginError, "load_local_plugin": load_local_plugin}[name]
    raise AttributeError(name)
