"""Core-owned capability brokers for untrusted plugin hosts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from penhin.auth.secrets import safe_value
from penhin.result import Result
from penhin.tools.builtin.files import run_read
from penhin.tools.execution import PermissionPolicy


CAPABILITIES = {"workspace.read", "network.fetch", "credential.handle", "state"}


@dataclass
class PluginCapabilityBroker:
    """Enforce declared capabilities without exposing core secrets to a plugin."""

    plugin_id: str
    declared: set[str]
    policy: PermissionPolicy
    credential_handles: dict[str, str] = field(default_factory=dict)
    _state: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def request(self, capability: str, operation: str, payload: dict[str, Any]) -> Result:
        if capability not in CAPABILITIES or capability not in self.declared:
            return self._blocked(capability, operation, "undeclared_capability")
        permission = f"plugin:{self.plugin_id}:{capability}"
        if permission in self.policy.deny or (permission not in self.policy.allow and capability not in self.policy.allow):
            return self._blocked(capability, operation, "capability_not_allowed")
        self.audit.append({"plugin": self.plugin_id, "capability": capability, "operation": operation})
        self._emit(capability, operation, "allowed")
        if capability == "workspace.read" and operation == "read":
            return run_read(str(payload.get("path", "")), payload.get("limit"))
        if capability == "network.fetch" and operation == "get":
            return self._fetch(str(payload.get("url", "")))
        if capability == "credential.handle" and operation == "get":
            handle = str(payload.get("name", ""))
            if handle not in self.credential_handles:
                return Result.failure("Credential handle is unavailable", code="credential_unavailable")
            return Result.success(json.dumps({"handle": handle}), data={"handle": handle})
        if capability == "state":
            return self._state_request(operation, payload)
        return Result.failure("Unsupported capability operation", code="unsupported_capability_operation")

    def _blocked(self, capability: str, operation: str, code: str) -> Result:
        self.audit.append({"plugin": self.plugin_id, "capability": capability, "operation": operation, "blocked": code})
        self._emit(capability, operation, "blocked", code)
        return Result.failure(f"Plugin capability blocked: {capability}", code=code)

    def _emit(self, capability: str, operation: str, status: str, code: str = "") -> None:
        from penhin.evaluation.observer import emit
        emit("plugin_capability", plugin=self.plugin_id, capability=capability, operation=operation, status=status, code=code)

    def _fetch(self, url: str) -> Result:
        if not url.startswith(("https://", "http://")):
            return Result.failure("Only HTTP(S) URLs are allowed", code="invalid_network_url")
        try:
            response = httpx.get(url, timeout=10, follow_redirects=False)
            return Result.success(response.text[:50_000], status=response.status_code, url=str(response.url))
        except httpx.HTTPError as error:
            return Result.failure(f"Network request failed: {safe_value(str(error))}", code="network_error")

    def _state_request(self, operation: str, payload: dict[str, Any]) -> Result:
        scope = str(payload.get("scope", "session"))
        if scope not in {"global", "project", "session"}:
            return Result.failure("Invalid plugin state scope", code="invalid_state_scope")
        bucket = self._state.setdefault((self.plugin_id, scope), {})
        key = str(payload.get("key", ""))
        if not key:
            return Result.failure("State key is required", code="missing_state_key")
        if operation == "get":
            return Result.success(json.dumps(safe_value({"value": bucket.get(key)})), data=safe_value({"value": bucket.get(key)}))
        if operation == "set":
            bucket[key] = payload.get("value")
            return Result.success("Plugin state saved")
        return Result.failure("Unsupported state operation", code="unsupported_capability_operation")
