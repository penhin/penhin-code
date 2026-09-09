from pathlib import Path

import pytest

from penhin.plugins import PluginError, load_local_plugin
from penhin.tools.execution import PermissionPolicy, run_tool


def write_fixture(root: Path, body: str = "return {'echo': value}") -> None:
    (root / "penhin-plugin.yaml").write_text(
        """api_version: 1
name: sample
tools:
  - name: echo
    description: Echo a supplied value from the fixture plugin.
    entrypoint: plugin:echo
    input_schema:
      type: object
      properties:
        value: {type: string}
      required: [value]
""",
        encoding="utf-8",
    )
    (root / "plugin.py").write_text(
        "from penhin.plugins.sdk import tool\nfrom penhin_plugin_sdk import capability\n\n"
        "@tool('Echo a supplied value from the fixture plugin.', {'type': 'object'})\n"
        f"def echo(value):\n    {body}\n",
        encoding="utf-8",
    )


def test_local_plugin_runs_in_dedicated_host_and_registers_namespaced_tool(tmp_path: Path) -> None:
    write_fixture(tmp_path)
    plugin = load_local_plugin(tmp_path)
    try:
        catalog = plugin.catalog()
        spec = catalog.get("sample__echo")
        result = run_tool("sample__echo", {"value": "hello"}, PermissionPolicy({"sample__echo"}), catalog=catalog)
        assert plugin.host.process.poll() is None
        assert plugin.environment.name != str(tmp_path)
        assert spec is not None
        assert spec.description == "Echo a supplied value from the fixture plugin."
        assert result.result.ok is True
        assert result.result.data == {"echo": "hello"}
    finally:
        plugin.close()


def test_plugin_failure_is_reported_without_stopping_the_host(tmp_path: Path) -> None:
    write_fixture(tmp_path, "raise RuntimeError('fixture failure')")
    plugin = load_local_plugin(tmp_path)
    try:
        catalog = plugin.catalog()
        result = run_tool("sample__echo", {"value": "hello"}, PermissionPolicy({"sample__echo"}), catalog=catalog)
        assert result.result.ok is False
        assert result.result.meta["code"] == "plugin_tool_error"
        assert plugin.host.process.poll() is None
    finally:
        plugin.close()


def test_plugin_requires_supported_api_version(tmp_path: Path) -> None:
    write_fixture(tmp_path)
    manifest = tmp_path / "penhin-plugin.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("api_version: 1", "api_version: 2"), encoding="utf-8")
    with pytest.raises(PluginError, match="api_version"):
        load_local_plugin(tmp_path)


def test_plugin_sdk_capability_request_crosses_the_host_boundary(tmp_path: Path) -> None:
    write_fixture(tmp_path, "return capability('state', 'set', scope='session', key='echo', value=value)")
    manifest = tmp_path / "penhin-plugin.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("name: sample", "name: sample\ncapabilities: [state]"), encoding="utf-8")
    plugin = load_local_plugin(tmp_path)
    try:
        catalog = plugin.catalog()
        result = run_tool("sample__echo", {"value": "hello"}, PermissionPolicy({"sample__echo"}), catalog=catalog)
        assert result.result.ok is True
        assert plugin.broker.audit[-1]["capability"] == "state"
    finally:
        plugin.close()
