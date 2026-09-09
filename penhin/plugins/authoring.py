from __future__ import annotations
import shutil
import subprocess
import time
from pathlib import Path
import yaml


TEMPLATE = """api_version: 1
name: {name}
capabilities: []
tools:
  - name: hello
    description: Say hello.
    entrypoint: plugin:hello
"""


def init(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "penhin-plugin.yaml").write_text(TEMPLATE.format(name=name), encoding="utf-8")
    (path / "plugin.py").write_text("def hello():\n    return {'message': 'hello'}\n", encoding="utf-8")


def check(path: Path) -> list[str]:
    manifest = yaml.safe_load((path / "penhin-plugin.yaml").read_text(encoding="utf-8")) or {}
    errors = []
    if manifest.get("api_version") not in {1, "1"}: errors.append("incompatible api_version")
    capabilities = manifest.get("capabilities", [])
    if len(capabilities) != len(set(capabilities)): errors.append("duplicate capability")
    tools = manifest.get("tools", [])
    names = [item.get("name") for item in tools if isinstance(item, dict)]
    if len(names) != len(set(names)): errors.append("duplicate contribution")
    for item in tools:
        if not isinstance(item, dict) or not all(item.get(key) for key in ("name", "description", "entrypoint")):
            errors.append("invalid tool declaration")
    return errors


def pack(path: Path, destination: Path) -> Path:
    errors = check(path)
    if errors: raise ValueError("; ".join(errors))
    archive = shutil.make_archive(str(destination), "zip", path)
    return Path(archive)


class DevelopmentServices:
    """Safe development doubles; no workspace mutation, network, or raw secrets."""
    def read_workspace(self, path: str) -> dict: return {"path": path, "content": "<mock>"}
    def fetch(self, url: str) -> dict: return {"url": url, "status": 200, "body": "<mock>"}
    def credential(self, name: str) -> dict: return {"handle": f"mock:{name}"}


def dev_changed(path: Path, previous_mtime: float) -> bool:
    return max(item.stat().st_mtime for item in path.rglob("*.py")) > previous_mtime


def test(path: Path) -> None:
    errors = check(path)
    if errors: raise ValueError("; ".join(errors))
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=path, capture_output=True, text=True)
    if result.returncode not in {0, 5}: raise RuntimeError(result.stdout + result.stderr)


def publish(path: Path, destination: Path) -> Path:
    return pack(path, destination)
