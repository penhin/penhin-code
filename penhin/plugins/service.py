from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from penhin.result import Result
from penhin.plugins.capabilities import PluginCapabilityBroker
from penhin.plugins.installation import verify_plugin_artifact
from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution import PermissionPolicy
from penhin.tools.types import ToolApproval, ToolCategory, ToolSpec


class PluginError(ValueError):
    pass


@dataclass(frozen=True)
class PluginTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    entrypoint: str


@dataclass(frozen=True)
class PluginManifest:
    name: str
    tools: tuple[PluginTool, ...]
    capabilities: frozenset[str]


def read_local_plugin_manifest(path: str | Path, expected_name: str | None = None) -> PluginManifest:
    """Validate a local artifact without creating a host or virtual environment."""
    root = Path(path).resolve()
    manifest_path = root / "penhin-plugin.yaml"
    if not manifest_path.is_file():
        raise PluginError("Missing penhin-plugin.yaml")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise PluginError(f"Invalid plugin manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise PluginError("Plugin manifest must be an object")
    name = manifest.get("name")
    tools = manifest.get("tools")
    declared = manifest.get("capabilities", [])
    api_version = manifest.get("api_version")
    if api_version not in {1, "1"}:
        raise PluginError("Plugin requires compatible api_version: 1")
    if not isinstance(name, str) or not name.replace("_", "").isalnum() or not isinstance(tools, list):
        raise PluginError("Manifest requires an alphanumeric name and a tools list")
    if expected_name is not None and name != expected_name:
        raise PluginError(f"Manifest name {name!r} does not match configured Plugin {expected_name!r}")
    if not isinstance(declared, list) or not all(isinstance(capability, str) for capability in declared):
        raise PluginError("Manifest capabilities must be a list of strings")
    parsed: list[PluginTool] = []
    for item in tools:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("name", "description", "entrypoint")):
            raise PluginError("Each tool requires name, description, and module:function entrypoint")
        if not item["name"].replace("_", "").isalnum():
            raise PluginError("Tool names must be alphanumeric with optional underscores")
        schema = item.get("input_schema", {"type": "object", "properties": {}, "required": []})
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise PluginError(f"Tool {item['name']} has an invalid input_schema")
        parsed.append(PluginTool(item["name"], item["description"], schema, item["entrypoint"]))
    return PluginManifest(name, tuple(parsed), frozenset(declared))


class LocalPluginLoader:
    """Adapter that defers host construction until PluginRuntime activation."""

    def __init__(self, trust_roots: dict[str, str] | None = None) -> None:
        self._trust_roots = trust_roots

    def validate(self, registration) -> Result:
        try:
            manifest = read_local_plugin_manifest(registration.source, expected_name=registration.name)
            allowed = registration.config.get("capabilities", list(manifest.capabilities))
            if not isinstance(allowed, list) or not all(isinstance(capability, str) for capability in allowed):
                raise PluginError("Plugin config capabilities must be a list of strings")
            artifact = verify_plugin_artifact(Path(registration.source), self._trust_roots) if self._trust_roots is not None else None
        except PluginError as error:
            return Result.failure(str(error), code="plugin_manifest_invalid")
        except ValueError as error:
            return Result.failure(str(error), code="plugin_artifact_untrusted")
        digest = artifact.digest if artifact is not None else hashlib.sha256((Path(registration.source).resolve() / "penhin-plugin.yaml").read_bytes()).hexdigest()
        return Result.success(data={
            "identity": artifact.resolved if artifact is not None else str(Path(registration.source).resolve()),
            "digest": digest,
            "capabilities": sorted(manifest.capabilities),
        })

    def load(self, registration) -> "LocalPlugin":
        manifest = read_local_plugin_manifest(registration.source, expected_name=registration.name)
        allowed = set(registration.config.get("capabilities", manifest.capabilities))
        capability_policy = PermissionPolicy(allow=set(manifest.capabilities) & allowed)
        return load_local_plugin(registration.source, capability_policy, expected_name=registration.name)


class PluginHost:
    """A JSON-lines host that keeps plugin imports and failures out of Penhin."""

    def __init__(self, python: Path, root: Path, broker: PluginCapabilityBroker) -> None:
        self.broker = broker
        script = (
            "import importlib,json,sys,types; root,core=sys.argv[1:]; sys.path[:0]=[root,core]; sdk=types.ModuleType('penhin_plugin_sdk'); "
            "\ndef capability(capability,operation,**payload):\n print(json.dumps({'type':'capability','capability':capability,'operation':operation,'payload':payload}),flush=True); response=json.loads(sys.stdin.readline());\n if not response.get('ok'): raise RuntimeError(response.get('error','capability blocked')); return response.get('data')\n"
            "sdk.capability=capability; sys.modules['penhin_plugin_sdk']=sdk; "
            "\nfor line in sys.stdin:\n"
            " try:\n  r=json.loads(line); m,n=r['entrypoint'].split(':',1); v=getattr(importlib.import_module(m),n)(**r['input']); print(json.dumps({'type':'result','ok':True,'value':v},default=str),flush=True)\n"
            " except Exception as e: print(json.dumps({'type':'result','ok':False,'error':str(e)}),flush=True)\n"
        )
        self.process = subprocess.Popen(
            [str(python), "-u", "-c", script, str(root), str(Path(__file__).resolve().parents[2])], text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def call(self, entrypoint: str, tool_input: dict[str, Any]) -> Result:
        if self.process.poll() is not None or self.process.stdin is None or self.process.stdout is None:
            return Result.failure("Plugin host is not running", code="plugin_host_unavailable")
        try:
            self.process.stdin.write(json.dumps({"entrypoint": entrypoint, "input": tool_input}) + "\n")
            self.process.stdin.flush()
            response = json.loads(self.process.stdout.readline())
            while response.get("type") == "capability":
                result = self.broker.request(response["capability"], response["operation"], response.get("payload", {}))
                self.process.stdin.write(json.dumps({"ok": result.ok, "data": result.data, "error": result.error}) + "\n")
                self.process.stdin.flush()
                response = json.loads(self.process.stdout.readline())
        except (OSError, json.JSONDecodeError) as error:
            return Result.failure(f"Plugin host protocol failed: {error}", code="plugin_host_protocol_error")
        if not response.get("ok"):
            return Result.failure(f"Plugin tool failed: {response.get('error', 'unknown error')}", code="plugin_tool_error")
        return Result.success(json.dumps(response["value"], ensure_ascii=False, default=str), data=response["value"])

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=2)


@dataclass
class LocalPlugin:
    name: str
    root: Path
    environment: TemporaryDirectory[str]
    host: PluginHost
    tools: tuple[PluginTool, ...]
    broker: PluginCapabilityBroker

    def catalog(self) -> ToolCatalog:
        return ToolCatalog([
            ToolSpec(
                name=f"{self.name}__{tool.name}", description=tool.description,
                input_schema=tool.input_schema, category=ToolCategory.readonly,
                handler=lambda _tool=tool, **kwargs: self.host.call(_tool.entrypoint, kwargs),
                parallel_safe=False, available_to_child=False, available_to_parent=True,
                approval=ToolApproval(),
            )
            for tool in self.tools
        ])

    def close(self) -> None:
        try:
            self.host.close()
        finally:
            self.environment.cleanup()


def load_local_plugin(
    path: str | Path,
    policy: PermissionPolicy | None = None,
    expected_name: str | None = None,
) -> LocalPlugin:
    root = Path(path).resolve()
    manifest = read_local_plugin_manifest(root, expected_name=expected_name)
    environment = TemporaryDirectory(prefix=f"penhin-plugin-{manifest.name}-")
    try:
        venv.EnvBuilder(with_pip=False).create(environment.name)
        executable = Path(environment.name) / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        policy = policy or PermissionPolicy(set(manifest.capabilities))
        broker = PluginCapabilityBroker(manifest.name, set(manifest.capabilities), policy)
        return LocalPlugin(manifest.name, root, environment, PluginHost(executable, root, broker), manifest.tools, broker)
    except BaseException:
        environment.cleanup()
        raise
