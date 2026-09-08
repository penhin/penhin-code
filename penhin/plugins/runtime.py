"""Governed Plugin lifecycle and explicit bulk reloads for one Penhin run."""
from __future__ import annotations
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class InputEnricherDeclaration:
    event_kinds: tuple[str, ...] = ("*",)
    mime_types: tuple[str, ...] = ("*",)
    parallel_safe: bool = False
    required_eligible: bool = False


@dataclass(frozen=True)
class InputRequirement:
    plugin: str
    event_kind: str = "*"
    mime_type: str = "*"

class PluginRuntime:
    """Owns discovery, activation, immutable catalog generations, and reload."""
    def __init__(self, base_catalog: ToolCatalog, registrations: list[PluginRegistration], loader: PluginLoader, *, manager: Any = None, audit_path: Path | None = None, cancellation_deadline: float = 2.0, input_policy: dict[str, Any] | None = None) -> None:
        self._base_catalog, self._loader, self._manager = base_catalog, loader, manager
        self._audit_path, self._cancellation_deadline = audit_path, cancellation_deadline
        self._registrations = self._unique(registrations)
        self._available: set[str] = set(); self._active: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}; self._diagnostics: list[dict[str, str]] = []
        self._audit: list[dict[str, Any]] = []; self._catalog = base_catalog
        self._generations: dict[str, PluginGeneration] = {}
        self._withdrawn_input_generations: set[tuple[str, int]] = set()
        self._input_policy = dict(input_policy or {})
        self._input_bypass: dict[tuple[str, str, str], str] = {}

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

    def emergency_bypass_input_requirement(self, plugin: str, event_kind: str, mime_type: str, *, reason: str) -> Result:
        """Explicitly bypass one project-required contribution and audit the exception."""
        if not reason:
            return Result.failure("Emergency bypass requires a reason", code="input_bypass_reason_required")
        key = (plugin, event_kind, mime_type)
        self._input_bypass[key] = reason
        self._record_audit({"event": "input_enrichment_emergency_bypass", "plugin": plugin, "event_kind": event_kind, "mime_type": mime_type, "reason": reason})
        return Result.success(data={"bypassed": key})

    @staticmethod
    def _enricher_declaration(plugin: Any) -> InputEnricherDeclaration:
        declaration = getattr(plugin, "input_enricher", {})
        if declaration is None: declaration = {}
        if not isinstance(declaration, dict):
            raise ValueError("Input Enricher declaration must be an object")
        event_kinds = declaration.get("event_kinds", ["*"])
        mime_types = declaration.get("mime_types", ["*"])
        if not all(isinstance(item, str) for item in event_kinds) or not all(isinstance(item, str) for item in mime_types):
            raise ValueError("Input Enricher event and MIME declarations must be strings")
        return InputEnricherDeclaration(tuple(event_kinds), tuple(mime_types), declaration.get("parallel_safe", False), declaration.get("required_eligible", False))

    @staticmethod
    def _matches(declaration: InputEnricherDeclaration, event: InputEvent) -> bool:
        if "*" not in declaration.event_kinds and event.kind not in declaration.event_kinds: return False
        mimes = declaration.mime_types
        return "*" in mimes or any(attachment.mime_type in mimes for attachment in event.attachments)

    def _required(self, name: str, declaration: InputEnricherDeclaration, event: InputEvent) -> tuple[bool, str | None]:
        for rule in self._input_policy.get("required", []):
            if not isinstance(rule, dict): continue
            requirement = InputRequirement(rule.get("plugin", ""), rule.get("event_kind", "*"), rule.get("mime_type", "*"))
            if requirement.plugin != name: continue
            kind, mime = requirement.event_kind, requirement.mime_type
            if kind not in {"*", event.kind}: continue
            if mime != "*" and not any(item.mime_type == mime for item in event.attachments): continue
            if not declaration.required_eligible: continue
            if (name, kind, mime) in self._input_bypass: return False, self._input_bypass[(name, kind, mime)]
            return True, None
        return False, None

    def _display_mode(self, name: str, event: InputEvent) -> str:
        """Project policy, never a Plugin, decides model submission mode."""
        for rule in self._input_policy.get("display", []):
            if not isinstance(rule, dict) or rule.get("plugin") != name: continue
            kind, mime, mode = rule.get("event_kind", "*"), rule.get("mime_type", "*"), rule.get("mode", "submit")
            if mode not in {"submit", "display"}: raise ValueError("Input display policy mode must be 'submit' or 'display'")
            if kind in {"*", event.kind} and (mime == "*" or any(item.mime_type == mime for item in event.attachments)): return mode
        return "submit"

    def process_input(self, event: InputEvent) -> Result:
        """Run active Input Enrichers against one immutable Plugin generation snapshot."""
        if not isinstance(event, InputEvent):
            return Result.failure("PluginRuntime input requires an InputEvent", code="invalid_input_event")
        if not event.attachments_are_live():
            return Result.failure("Input event contains expired attachments", code="attachment_expired")
        handles = tuple(AttachmentHandle(attachment) for attachment in event.attachments)
        configured_order = self._input_policy.get("order", [])
        if not isinstance(configured_order, list) or not all(isinstance(name, str) for name in configured_order):
            return Result.failure("Input enrichment policy order must be a list of Plugin IDs", code="invalid_input_policy")
        rank = {name: index for index, name in enumerate(configured_order)}
        active = []
        for name, plugin in self._active.items():
            if not callable(getattr(plugin, "enrich_input", None)): continue
            try: declaration = self._enricher_declaration(plugin)
            except ValueError as error:
                self._diagnostics.append({"plugin": name, "stage": "input_enrichment", "error": str(error)})
                continue
            if self._matches(declaration, event): active.append((name, plugin, self._generations[name], declaration))
        active.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0]))
        supplements = []
        generations = {}
        diagnostics = []
        def invoke(item: tuple[str, Any, PluginGeneration, InputEnricherDeclaration]):
            name, plugin, generation, declaration = item
            generations[name] = generation.number
            try:
                supplement = supplement_from_result(name, generation.number, plugin.enrich_input(InputEnrichmentEvent(event.kind, event.text), handles))
                if (name, generation.number) in self._withdrawn_input_generations:
                    return None, {"plugin": name, "stage": "input_enrichment", "error": "Contribution withdrawn during enrichment"}, False
                supplement = replace(supplement, display_mode=self._display_mode(name, event))
            except Exception as error:
                return None, {"plugin": name, "stage": "input_enrichment", "error": str(error)}, self._required(name, declaration, event)[0]
            return supplement, None, False

        index = 0
        while index < len(active):
            item = active[index]
            batch = [item]
            index += 1
            if item[3].parallel_safe:
                while index < len(active) and active[index][3].parallel_safe:
                    batch.append(active[index]); index += 1
            if len(batch) == 1:
                outcomes = [invoke(batch[0])]
            else:
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    outcomes = list(executor.map(invoke, batch))
            for supplement, diagnostic, required in outcomes:
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                    if required:
                        self._diagnostics.extend(diagnostics)
                        return Result.failure("Required Input Enricher failed", code="required_input_enrichment_failed", data={"diagnostics": diagnostics})
                elif supplement is not None:
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
            authorization_for = getattr(self._manager, "authorization_for", None)
            if callable(authorization_for):
                decision = authorization_for(registration.name, data["identity"], data["digest"], data["capabilities"])
                if decision not in {"approved", "inherited"}:
                    return Result.failure(f"Plugin Artifact authorization is {decision}", code=f"plugin_authorization_{decision}")
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

    def _load_and_bind_plugin(self, name: str, registration: PluginRegistration) -> Any:
        """Create one Plugin's host and bind only its runtime-assigned identity."""
        plugin = self._loader.load(registration)
        bind_plugin_id = getattr(plugin, "bind_plugin_id", None)
        if callable(bind_plugin_id): bind_plugin_id(name)
        return plugin

    def _assert_independent_host(self, name: str, plugin: Any) -> None:
        identity = getattr(plugin, "host_identity", None)
        if identity is not None and any(getattr(active, "host_identity", object()) == identity for active_name, active in self._active.items() if active_name != name):
            raise ValueError("Each active Plugin requires an independent host")

    def activate(self, name: str) -> Result:
        if name not in self._available: return Result.failure(f"Plugin {name!r} is not available", code="plugin_unavailable")
        if name in self._active: return Result.success(data={"plugin": name, "active": True})
        plugin = None
        try:
            plugin = self._load_and_bind_plugin(name, self._registrations[name])
            self._assert_independent_host(name, plugin)
            proposed = plugin.catalog()
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
        generation = self._generations.pop(name, None)
        if generation is not None: self._withdrawn_input_generations.add((name, generation.number))
        self._publish()
        try: self._stop_old(plugin)
        except Exception as error:
            self._diagnostics.append({"plugin": name, "stage": "deactivation", "error": str(error)})
            return Result.failure(f"Unable to close Plugin {name!r}: {error}", code="plugin_deactivation_failed")
        return Result.success(data={"plugin": name, "active": False})

    def revoke(self, name: str) -> Result:
        """Immediately withdraw an authorized Plugin from this run's catalog."""
        result = self.deactivate(name) if name in self._active else Result.success(data={"plugin": name, "active": False})
        self._available.discard(name)
        self._record_audit({"event": "plugin_authorization_revoked", "plugin": name})
        return result

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
                        replacement = self._load_and_bind_plugin(name, registration)
                        self._assert_independent_host(name, replacement)
                        proposed = replacement.catalog()
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
        for name, plugin in plugins:
            try: self._stop_old(plugin)
            except Exception as error: self._diagnostics.append({"plugin": name, "stage": "cleanup", "error": str(error)})
