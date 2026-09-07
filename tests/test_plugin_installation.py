from pathlib import Path

from penhin.plugins.installation import resolve_plugin_source, update_project_lock


def test_local_source_is_hashed_and_project_lock_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "penhin-plugin.yaml").write_text("api_version: 1\nname: sample\ntools: []\n", encoding="utf-8")
    artifact = resolve_plugin_source(str(source), tmp_path / "installed")
    lock = update_project_lock("sample", artifact, tmp_path / "project" / "plugins.lock.json")
    assert artifact.digest
    assert lock["plugins"]["sample"]["resolved"] == str(source.resolve())


def test_pypi_source_requires_an_exact_version(tmp_path: Path) -> None:
    try:
        resolve_plugin_source("example-plugin", tmp_path / "installed")
    except ValueError as error:
        assert "exact version" in str(error)
    else:
        raise AssertionError("unpinned PyPI source was accepted")
