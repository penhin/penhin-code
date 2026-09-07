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
