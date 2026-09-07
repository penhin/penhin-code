"""Reproducible project-scoped plugin source resolution and locking."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from penhin.infrastructure.atomic_io import write_json_atomic


LOCK_FILE = Path(".penhin/plugins.lock.json")


@dataclass(frozen=True)
class PluginArtifact:
    source: str
    resolved: str
    digest: str
    dependencies: tuple[str, ...] = ()


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def resolve_plugin_source(source: str, destination: Path) -> PluginArtifact:
    """Materialize a local path, Git revision, or exact PyPI requirement."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "artifact"
    if source.startswith(("git+", "https://", "ssh://")):
        subprocess.run(["git", "clone", "--depth", "1", source.removeprefix("git+"), str(target)], check=True, capture_output=True, text=True)
        revision = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        return PluginArtifact(source, revision, _digest_tree(target))
    candidate = Path(source).expanduser().resolve()
    if candidate.is_dir():
        shutil.copytree(candidate, target)
        return PluginArtifact(source, str(candidate), _digest_tree(target))
    if "==" not in source:
        raise ValueError("PyPI plugin sources must pin an exact version: name==version")
    report = destination / "pip-report.json"
    subprocess.run([
        "python", "-m", "pip", "download", "--no-deps", "--dest", str(target),
        "--report", str(report), source,
    ], check=True, capture_output=True, text=True)
    data = json.loads(report.read_text(encoding="utf-8"))
    dependencies = tuple(sorted(item["metadata"]["name"] + "==" + item["metadata"]["version"] for item in data.get("install", [])))
    return PluginArtifact(source, source, _digest_tree(target), dependencies)


def update_project_lock(name: str, artifact: PluginArtifact, lock_file: Path = LOCK_FILE) -> dict:
    current = json.loads(lock_file.read_text(encoding="utf-8")) if lock_file.exists() else {"version": 1, "plugins": {}}
    plugins = dict(current.get("plugins", {}))
    plugins[name] = asdict(artifact)
    current = {"version": 1, "plugins": dict(sorted(plugins.items()))}
    write_json_atomic(lock_file, current, sort_keys=True, trailing_newline=True)
    return current
