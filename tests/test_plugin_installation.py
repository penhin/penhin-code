from pathlib import Path

import hashlib
import hmac
import json

import pytest

from penhin.plugins.installation import (
    resolve_plugin_source,
    update_project_lock,
    verify_plugin_artifact,
)


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


def test_official_artifact_verification_checks_tree_locks_and_signature(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "plugin.py").write_text("def hello(): pass\n", encoding="utf-8")
    (artifact / "deps.lock").write_text("locked", encoding="utf-8")
    (artifact / "model.bin").write_text("locked", encoding="utf-8")
    digest = hashlib.sha256()
    for name in ("deps.lock", "model.bin", "plugin.py"):
        digest.update(name.encode()); digest.update((artifact / name).read_bytes())
    record = {
        "publisher": "penhin-official",
        "tree_digest": digest.hexdigest(),
        "dependencies": {"deps.lock": hashlib.sha256(b"locked").hexdigest()},
        "model_assets": {"model.bin": hashlib.sha256(b"locked").hexdigest()},
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["signature"] = hmac.new(b"official-root", payload, hashlib.sha256).hexdigest()
    (artifact / "penhin-artifact.json").write_text(json.dumps(record), encoding="utf-8")

    verified = verify_plugin_artifact(artifact, {"penhin-official": "official-root"})

    assert verified.digest
    (artifact / "model.bin").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="tree digest"):
        verify_plugin_artifact(artifact, {"penhin-official": "official-root"})


def test_artifact_rejects_an_untrusted_publisher(tmp_path: Path) -> None:
    (tmp_path / "penhin-artifact.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="trust root"):
        verify_plugin_artifact(tmp_path, {})


def test_installation_requires_verification_when_trust_roots_are_supplied(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.mkdir()
    (source / "penhin-plugin.yaml").write_text("api_version: 1\nname: sample\ntools: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity record"):
        resolve_plugin_source(str(source), tmp_path / "installed", trust_roots={"official": "root"})
