"""Governed plugin loading primitives."""

__all__ = ["Attachment", "AttachmentSession", "ContextSupplement", "InputEditor", "InputEnrichmentEvent", "InputEvent", "InputSubmission", "LocalPlugin", "LocalPluginLoader", "PluginError", "PluginGeneration", "PluginRegistration", "PluginRuntime", "load_local_plugin"]


def __getattr__(name: str):
    if name in {"PluginGeneration", "PluginRegistration", "PluginRuntime"}:
        from .runtime import PluginGeneration, PluginRegistration, PluginRuntime
        return {"PluginGeneration": PluginGeneration, "PluginRegistration": PluginRegistration, "PluginRuntime": PluginRuntime}[name]
    if name in {"Attachment", "AttachmentSession", "ContextSupplement", "InputEditor", "InputEnrichmentEvent", "InputEvent", "InputSubmission"}:
        from .input import Attachment, AttachmentSession, ContextSupplement, InputEditor, InputEnrichmentEvent, InputEvent, InputSubmission
        return {"Attachment": Attachment, "AttachmentSession": AttachmentSession, "ContextSupplement": ContextSupplement, "InputEditor": InputEditor, "InputEnrichmentEvent": InputEnrichmentEvent, "InputEvent": InputEvent, "InputSubmission": InputSubmission}[name]
    if name in __all__:
        from .service import LocalPlugin, LocalPluginLoader, PluginError, load_local_plugin
        return {"LocalPlugin": LocalPlugin, "LocalPluginLoader": LocalPluginLoader, "PluginError": PluginError, "load_local_plugin": load_local_plugin}[name]
    raise AttributeError(name)
