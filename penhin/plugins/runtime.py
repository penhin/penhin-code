"""Governed Plugin lifecycle and explicit bulk reloads for one Penhin run."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from penhin.infrastructure.atomic_io import write_json_atomic
from penhin.plugins.input import AttachmentHandle, InputEnrichmentEvent, InputEvent, InputSubmission, supplement_from_result
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog

@dataclass(frozen=True)
class PluginRegistration:
    name: str
    source: str
    config: dict[str, Any] = field(default_factory=dict)

class PluginLoader(Protocol):
    def validate(self, registration: PluginRegistration) -> Result: ...
    def load(self, registration: PluginRegistration) -> Any: ...

@dataclass(frozen=True)
class PluginGeneration:
    """An immutable publication unit for all of one Plugin's contributions."""
    number: int
    catalog: ToolCatalog
    contributions: Any | None = None

class PluginRuntime:
    """Owns discovery, activation, immutable catalog generations, and reload."""
    def __init__(self, base_catalog: ToolCatalog, registrations: list[PluginRegistration], loader: PluginLoader, *, manager: Any = None, audit_path: Path | None = None, cancellation_deadline: float = 2.0) -> None:
        self._base_catalog, self._loader, self._manager = base_catalog, loader, manager
        self._audit_path, self._cancellation_deadline = audit_path, cancellation_deadline
        self._registrations = self._unique(registrations)
        self._available: set[str] = set(); self._active: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}; self._diagnostics: list[dict[str, str]] = []
        self._audit: list[dict[str, Any]] = []; self._catalog = base_catalog
        self._generations: dict[str, PluginGeneration] = {}

    @staticmethod
    def _unique(registrations: list[PluginRegistration]) -> dict[str, PluginRegistration]:
        result = {item.name: item for item in registrations}
        if len(result) != len(registrations): raise ValueError("Plugin registrations must have unique names")
        return result

    @classmethod
    def from_manager(cls, base_catalog: ToolCatalog, manager: Any, loader: PluginLoader, *, audit_path: Path | None = None) -> "PluginRuntime":
        registrations, diagnostics = cls._read_manager(manager)
        runtime = cls(base_catalog, registrations, loader, manager=manager, audit_path=audit_path)
        runtime._diagnostics.extend(diagnostics)
        return runtime

    @staticmethod
    def _read_manager(manager: Any) -> tuple[list[PluginRegistration], list[dict[str, str]]]:
        registrations: list[PluginRegistration] = []; diagnostics: list[dict[str, str]] = []
        for name, entry in manager.effective().items():
            if not isinstance(entry, dict) or entry.get("enabled", True) is False: continue
            source, config = entry.get("source"), entry.get("config", {})
            if not isinstance(source, str) or not source: diagnostics.append({"plugin": str(name), "stage": "configuration", "error": "Plugin requires a non-empty source"})
            elif not isinstance(config, dict): diagnostics.append({"plugin": str(name), "stage": "configuration", "error": "Plugin config must be an object"})
            else: registrations.append(PluginRegistration(str(name), source, config))
        return registrations, diagnostics

    def registrations(self) -> tuple[PluginRegistration, ...]: return tuple(self._registrations.values())
    def available(self) -> tuple[str, ...]: return tuple(sorted(self._available))
    def active(self) -> tuple[str, ...]: return tuple(sorted(self._active))
    def catalog(self) -> ToolCatalog: return self._catalog
    def generation(self, name: str) -> PluginGeneration | None: return self._generations.get(name)
    def diagnostics(self) -> tuple[dict[str, str], ...]: return tuple(dict(item) for item in self._diagnostics)
    def audit_records(self) -> tuple[dict[str, Any], ...]: return tuple(dict(item) for item in self._audit)

    def process_input(self, event: InputEvent) -> Result:
        """Run active Input Enrichers against one immutable Plugin generation snapshot."""
        if not isinstance(event, InputEvent):
            return Result.failure("PluginRuntime input requires an InputEvent", code="invalid_input_event")
        if not event.attachments_are_live():
            return Result.failure("Input event contains expired attachments", code="attachment_expired")
        handles = tuple(AttachmentHandle(attachment) for attachment in event.attachments)
        active = tuple((name, self._active[name], self._generations[name]) for name in sorted(self._active))
        supplements = []
        generations = {}
        diagnostics = []
        for name, plugin, generation in active:
            enrich = getattr(plugin, "enrich_input", None)
            if not callable(enrich):
                continue
            generations[name] = generation.number
            try:
                supplement = supplement_from_result(name, generation.number, enrich(InputEnrichmentEvent(event.kind, event.text), handles))
            except Exception as error:
                diagnostics.append({"plugin": name, "stage": "input_enrichment", "error": str(error)})
                continue
            supplements.append(supplement)
        self._diagnostics.extend(diagnostics)
        return Result.success(data=InputSubmission(event.text, tuple(supplements), generations))

    def _validate(self, registration: PluginRegistration) -> Result:
        try: outcome = self._loader.validate(registration)
        except Exception as error: return Result.failure(str(error), code="plugin_discovery_failed")
        if outcome.ok:
            data = dict(outcome.data or {})
            data.setdefault("identity", str(Path(registration.source).expanduser().resolve()))
            data.setdefault("digest", registration.config.get("digest", ""))
            data.setdefault("capabilities", sorted(registration.config.get("capabilities", [])))
            self._metadata[registration.name] = data
        return outcome

    def discover(self) -> Result:
        for name, registration in self._registrations.items():
            outcome = self._validate(registration)
            if outcome.ok: self._available.add(name)
            else:
                self._available.discard(name); self._diagnostics.append({"plugin": name, "stage": "discovery", "error": outcome.error})
        return Result.success(data={"available": sorted(self._available)})

    def _publish(self) -> None:
        """Publish a new whole-catalog generation; old snapshots stay immutable."""
        self._catalog = self._base_catalog.merged(*(plugin.catalog() for plugin in self._active.values()))

    def activate(self, name: str) -> Result:
        if name not in self._available: return Result.failure(f"Plugin {name!r} is not available", code="plugin_unavailable")
        if name in self._active: return Result.success(data={"plugin": name, "active": True})
        plugin = None
        try:
            plugin = self._loader.load(self._registrations[name]); proposed = plugin.catalog()
            invalid = sorted(item for item in proposed.names() if not item.startswith(f"{name}__"))
            if invalid: raise ValueError(f"Non-namespaced tools: {', '.join(invalid)}")
            self._base_catalog.merged(*(p.catalog() for p in self._active.values()), proposed)
            self._active[name] = plugin
            previous = self._generations.get(name)
            self._generations[name] = PluginGeneration((previous.number if previous else 0) + 1, proposed, getattr(plugin, "contributions", None))
            self._publish()
        except Exception as error:
            if plugin is not None: self._close_plugin(name, plugin)
            self._diagnostics.append({"plugin": name, "stage": "activation", "error": str(error)})
            return Result.failure(f"Unable to activate Plugin {name!r}: {error}", code="plugin_tool_namespace_error" if "Non-namespaced" in str(error) else "plugin_activation_failed")
        return Result.success(data={"plugin": name, "active": True})

    def deactivate(self, name: str) -> Result:
        plugin = self._active.pop(name, None)
        if plugin is None: return Result.failure(f"Plugin {name!r} is not active", code="plugin_inactive")
        self._generations.pop(name, None); self._publish()
        try: plugin.close()
        except Exception as error:
            self._diagnostics.append({"plugin": name, "stage": "deactivation", "error": str(error)})
            return Result.failure(f"Unable to close Plugin {name!r}: {error}", code="plugin_deactivation_failed")
        return Result.success(data={"plugin": name, "active": False})

    def _authorized(self, old: dict[str, Any] | None, new: dict[str, Any], registration: PluginRegistration) -> str:
        configured = registration.config.get("authorized", True)
        if configured is False: return "denied"
        # Discovery is intentionally permissive and lazy.  Adoption during an
        # explicit reload is different: a newly configured Plugin must carry a
        # recorded approval instead of inheriting an implicit default.
        if old is None: return "approved" if registration.config.get("authorized") is True else "pending"
        changed = old.get("identity") != new.get("identity") or not set(new.get("capabilities", [])).issubset(old.get("capabilities", []))
        return "approved" if changed and registration.config.get("reauthorized", False) else "pending" if changed else "inherited"

    def _stop_old(self, plugin: Any) -> str:
        cancel = getattr(plugin, "cancel", None)
        if callable(cancel):
            try: cancel()
            except Exception: pass
        running = getattr(plugin, "running", None); deadline = time.monotonic() + self._cancellation_deadline
        while callable(running) and running() and time.monotonic() < deadline: time.sleep(0.01)
        if callable(running) and running():
            terminate = getattr(plugin, "terminate", None)
            if callable(terminate): terminate()
            else: plugin.close()
            return "forced_termination"
        plugin.close(); return "cancelled"

    def reload(self) -> Result:
        """Reconcile configured Plugins independently; this never acquires code."""
        if self._manager is None: return Result.failure("Plugin reload requires a configuration manager", code="plugin_reload_unavailable")
        batch: dict[str, Any] = {"batch_id": uuid.uuid4().hex, "plugins": [], "result": "success"}
        registrations, errors = self._read_manager(self._manager); self._diagnostics.extend(errors); fresh = self._unique(registrations)
        for name in sorted(set(self._registrations) - set(fresh)):
            active = name in self._active
            if active: self.deactivate(name)
            self._available.discard(name); self._metadata.pop(name, None); self._generations.pop(name, None)
            batch["plugins"].append({"plugin": name, "old_active": active, "result_code": "removed", "contribution": "withdrawn"})
        for name, registration in fresh.items():
            old_registration, old_plugin, old_meta = self._registrations.get(name), self._active.get(name), dict(self._metadata.get(name, {})); active = old_plugin is not None
            outcome = self._validate(registration); new_meta = self._metadata.get(name, {})
            record: dict[str, Any] = {"plugin": name, "old_digest": old_meta.get("digest", ""), "new_digest": new_meta.get("digest", ""), "identity": new_meta.get("identity", ""), "old_active": active, "cancellation": "not_applicable", "contribution": "unchanged"}
            if not outcome.ok:
                self._available.discard(name); record.update(authorization="not_applicable", result_code=outcome.meta.get("code", "validation_failed"))
            else:
                decision = self._authorized(old_meta if old_registration else None, new_meta, registration); record["authorization"] = decision
                if decision in {"pending", "denied"}:
                    if not active: self._available.discard(name)
                    record["result_code"] = f"authorization_{decision}"
                elif not active:
                    self._available.add(name); record.update(result_code="discovered", contribution="inactive")
                else:
                    replacement = None
                    try:
                        replacement = self._loader.load(registration); proposed = replacement.catalog()
                        invalid = sorted(item for item in proposed.names() if not item.startswith(f"{name}__"))
                        if invalid: raise ValueError(f"Non-namespaced tools: {', '.join(invalid)}")
                        self._base_catalog.merged(*(p.catalog() for key, p in self._active.items() if key != name), proposed)
                        record["cancellation"] = self._stop_old(old_plugin)
                        self._active[name] = replacement; self._registrations[name] = registration; self._available.add(name)
                        previous = self._generations.get(name)
                        self._generations[name] = PluginGeneration((previous.number if previous else 0) + 1, proposed, getattr(replacement, "contributions", None))
                        self._publish()
                        record.update(result_code="replaced", contribution="published")
                    except Exception as error:
                        if replacement is not None: self._close_plugin(name, replacement)
                        record.update(result_code="replacement_failed", error=str(error), contribution="retained")
                        self._diagnostics.append({"plugin": name, "stage": "reload", "error": str(error)})
            batch["plugins"].append(record)
        self._registrations = fresh | {name: self._registrations[name] for name in self._active if name not in fresh}
        if any(p["result_code"] in {"replacement_failed", "authorization_pending", "authorization_denied"} for p in batch["plugins"]): batch["result"] = "partial"
        self._record_audit(batch); return Result.success(data=batch)

    def _record_audit(self, batch: dict[str, Any]) -> None:
        self._audit.append(batch)
        if self._audit_path is not None:
            current = json.loads(self._audit_path.read_text(encoding="utf-8")) if self._audit_path.exists() else []
            write_json_atomic(self._audit_path, [*current, batch], sort_keys=True, trailing_newline=True)

    def _close_plugin(self, name: str, plugin: Any) -> None:
        try: plugin.close()
        except Exception as error: self._diagnostics.append({"plugin": name, "stage": "cleanup", "error": str(error)})

    def close(self) -> None:
        plugins, self._active = tuple(self._active.items()), {}
        self._generations.clear()
        self._publish()
        for name, plugin in plugins: self._close_plugin(name, plugin)
