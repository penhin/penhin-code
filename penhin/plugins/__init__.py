"""Governed plugin loading primitives."""

__all__ = ["LocalPlugin", "PluginError", "load_local_plugin"]


def __getattr__(name: str):
    if name in __all__:
        from .service import LocalPlugin, PluginError, load_local_plugin
        return {"LocalPlugin": LocalPlugin, "PluginError": PluginError, "load_local_plugin": load_local_plugin}[name]
    raise AttributeError(name)
