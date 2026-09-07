from pathlib import Path

from penhin.plugins import authoring
from penhin.plugins.installation import resolve_plugin_source, update_project_lock
from penhin.plugins.routing import PluginCapability, PluginRouter
from penhin.plugins.service import load_local_plugin
from penhin.tools.execution import PermissionPolicy, run_tool


def test_asr_fixture_author_to_governed_invocation_and_reproducible_lock(tmp_path: Path) -> None:
    source = tmp_path / "asr"
    authoring.init(source, "asr")
    (source / "penhin-plugin.yaml").write_text(
        """api_version: 1
name: asr
capabilities: [state]
tools:
  - name: transcribe
    description: Transcribe a short audio fixture.
    entrypoint: plugin:transcribe
    input_schema:
      type: object
      properties:
        audio: {type: string}
      required: [audio]
""", encoding="utf-8")
    (source / "plugin.py").write_text(
        "from penhin_plugin_sdk import capability\n"
        "def transcribe(audio):\n"
        "    capability('state', 'set', scope='project', key='last_audio', value=audio)\n"
        "    return {'text': 'fixture transcript', 'audio': audio}\n",
        encoding="utf-8")
    assert authoring.check(source) == []
    artifact = resolve_plugin_source(str(source), tmp_path / "artifacts")
    lock_file = tmp_path / "project" / "plugins.lock.json"
    first_lock = update_project_lock("asr", artifact, lock_file)
    second_lock = update_project_lock("asr", artifact, lock_file)
    assert first_lock == second_lock

    plugin = load_local_plugin(source)
    try:
        route = PluginRouter([PluginCapability("asr", ("audio",), ("transcribe",), plugin.catalog())]).route("transcribe audio")
        assert route.selected == "asr"
        result = run_tool("asr__transcribe", {"audio": "sample.wav"}, PermissionPolicy({"asr__transcribe"}), catalog=route.catalog)
        assert result.result.ok and result.result.data["text"] == "fixture transcript"
        assert plugin.broker._state[("asr", "project")]["last_audio"] == "sample.wav"
    finally:
        plugin.close()
