from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from penhin.infrastructure.atomic_io import write_json_atomic


class PluginManager:
    def __init__(self, global_file: Path, project_file: Path) -> None:
        self.global_file, self.project_file = global_file, project_file

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.exists(): return {"plugins": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"plugins": {}}

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        write_json_atomic(path, data, sort_keys=True, trailing_newline=True)

    def effective(self) -> dict[str, dict[str, Any]]:
        global_plugins = self._read(self.global_file).get("plugins", {})
        project_plugins = self._read(self.project_file).get("plugins", {})
        return {**global_plugins, **project_plugins}

    def install(self, name: str, source: str, scope: str = "project") -> None:
        path = self.project_file if scope == "project" else self.global_file
        data = self._read(path); plugins = dict(data.get("plugins", {}))
        plugins[name] = {"source": source, "enabled": True, "config": {}}
        self._write(path, {"plugins": plugins})

    def set_enabled(self, name: str, enabled: bool, scope: str = "project") -> None:
        path = self.project_file if scope == "project" else self.global_file
        data = self._read(path); plugins = dict(data.get("plugins", {}))
        if name not in plugins: raise KeyError(name)
        plugins[name] = {**plugins[name], "enabled": enabled}
        self._write(path, {"plugins": plugins})

    def configure(self, name: str, values: dict[str, Any], scope: str = "project") -> None:
        path = self.project_file if scope == "project" else self.global_file
        data = self._read(path); plugins = dict(data.get("plugins", {}))
        if name not in plugins: raise KeyError(name)
        plugins[name] = {**plugins[name], "config": {**plugins[name].get("config", {}), **values}}
        self._write(path, {"plugins": plugins})

    def remove(self, name: str, scope: str = "project") -> None:
        path = self.project_file if scope == "project" else self.global_file
        data = self._read(path); plugins = dict(data.get("plugins", {}))
        if name not in plugins: raise KeyError(name)
        del plugins[name]
        self._write(path, {"plugins": plugins})

    def update(self, name: str, source: str, scope: str = "project") -> None:
        path = self.project_file if scope == "project" else self.global_file
        data = self._read(path); plugins = dict(data.get("plugins", {}))
        if name not in plugins: raise KeyError(name)
        plugins[name] = {**plugins[name], "source": source}
        self._write(path, {"plugins": plugins})

    def add_project_trust_root(self, publisher: str, root: str) -> None:
        if not publisher or not root:
            raise ValueError("Publisher and trust root are required")
        data = self._read(self.project_file)
        roots = dict(data.get("publisher_trust_roots", {})); roots[publisher] = root
        self._write(self.project_file, {**data, "publisher_trust_roots": dict(sorted(roots.items()))})

    def project_trust_roots(self) -> dict[str, str]:
        roots = self._read(self.project_file).get("publisher_trust_roots", {})
        return dict(roots) if isinstance(roots, dict) else {}

    def authorize_artifact(self, name: str, source_identity: str, digest: str, capabilities: list[str]) -> None:
        if not digest:
            raise ValueError("Artifact digest is required")
        data = self._read(self.project_file); authorizations = dict(data.get("authorizations", {}))
        authorizations[name] = {"identity": source_identity, "digest": digest, "capabilities": sorted(set(capabilities)), "revoked": False}
        self._write(self.project_file, {**data, "authorizations": authorizations})

    def revoke_artifact(self, name: str) -> None:
        data = self._read(self.project_file); authorizations = dict(data.get("authorizations", {}))
        existing = dict(authorizations.get(name, {})); existing["revoked"] = True; authorizations[name] = existing
        self._write(self.project_file, {**data, "authorizations": authorizations})

    def authorization_for(self, name: str, source_identity: str, digest: str, capabilities: list[str]) -> str:
        record = self._read(self.project_file).get("authorizations", {}).get(name)
        if not isinstance(record, dict): return "pending"
        if record.get("revoked"): return "denied"
        if record.get("identity") != source_identity: return "pending"
        approved = set(record.get("capabilities", [])); requested = set(capabilities)
        if requested > approved: return "pending"
        return "approved" if record.get("digest") == digest and requested == approved else "inherited"

    def require_contribution(self, contribution: dict[str, str]) -> None:
        contribution_id = contribution.get("id", "")
        if not contribution_id:
            raise ValueError("Contribution ID is required")
        if not contribution.get("plugin"):
            raise ValueError("Plugin ID is required")
        data = self._read(self.project_file); required = list(data.get("required_contributions", []))
        if contribution not in required: required.append(dict(contribution))
        self._write(self.project_file, {**data, "required_contributions": sorted(required, key=lambda item: (item["plugin"], item["id"]))})

    def required_contributions(self) -> list[dict[str, str]]:
        required = self._read(self.project_file).get("required_contributions", [])
        return list(required) if isinstance(required, list) else []

    def eligible_contributions(self, contribution_id: str) -> list[dict[str, str]]:
        """Return project-configured Contributions eligible for concise require."""
        eligible: list[dict[str, str]] = []
        for plugin_name, plugin in self._read(self.project_file).get("plugins", {}).items():
            if not isinstance(plugin, dict): continue
            for contribution in plugin.get("config", {}).get("contributions", []):
                if isinstance(contribution, dict) and contribution.get("id") == contribution_id and contribution.get("required_eligible") is True:
                    eligible.append({"plugin": plugin_name, "id": contribution_id})
        return eligible
