"""Reproducible project-scoped plugin source resolution and locking."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from penhin.infrastructure.atomic_io import write_json_atomic


LOCK_FILE = Path(".penhin/plugins.lock.json")
# Official publishers are trusted by the runtime, independently of a project's
# local/Git publisher policy. Values are verification roots, never credentials.
OFFICIAL_PUBLISHER_TRUST_ROOTS = {"penhin-official": "penhin-official-v1"}


@dataclass(frozen=True)
class PluginArtifact:
    source: str
    resolved: str
    digest: str
    dependencies: tuple[str, ...] = ()
    publisher: str = ""


@dataclass(frozen=True)
class InstalledPluginArtifact:
    """A verified Artifact materialised in a durable Plugin store."""

    artifact: PluginArtifact
    path: Path


def _digest_tree(root: Path, *, exclude: set[str] | None = None) -> str:
    digest = hashlib.sha256()
    excluded = exclude or set()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts and item.name not in excluded):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _signed_payload(record: dict) -> bytes:
    return json.dumps(
        {key: value for key, value in record.items() if key != "signature"},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def verify_plugin_artifact(root: Path, trust_roots: dict[str, str]) -> PluginArtifact:
    """Verify a complete Artifact record against a project or official trust root.

    The signed record deliberately names every dependency and model asset.  They
    are checked before a host may consume the Artifact, so a copied directory is
    not implicitly trusted merely because its Plugin manifest parses.
    """
    root = root.resolve()
    record_path = root / "penhin-artifact.json"
    if not record_path.is_file():
        raise ValueError("Plugin Artifact is missing its integrity record")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Plugin Artifact has an invalid integrity record") from error
    publisher = record.get("publisher")
    if not isinstance(publisher, str) or publisher not in trust_roots:
        raise ValueError("Plugin Artifact publisher has no configured trust root")
    signature = record.get("signature")
    expected = hmac.new(trust_roots[publisher].encode("utf-8"), _signed_payload(record), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise ValueError("Plugin Artifact signature does not match its trust root")
    tree_digest = record.get("tree_digest")
    if not isinstance(tree_digest, str) or not hmac.compare_digest(tree_digest, _digest_tree(root, exclude={"penhin-artifact.json"})):
        raise ValueError("Plugin Artifact tree digest does not match")
    for kind, message in (("dependencies", "dependency"), ("model_assets", "model asset")):
        locked = record.get(kind)
        if not isinstance(locked, dict) or not locked:
            raise ValueError(f"Plugin Artifact requires locked {kind}")
        for relative, expected_content in locked.items():
            path = root / relative
            if not isinstance(relative, str) or not isinstance(expected_content, str) or not path.is_file():
                raise ValueError(f"Plugin Artifact {message} lock is invalid")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if not hmac.compare_digest(actual, expected_content):
                raise ValueError(f"Plugin Artifact {message} lock does not match")
    return PluginArtifact(str(root), str(root), tree_digest, tuple(sorted(record["dependencies"])), publisher)


def resolve_plugin_source(source: str, destination: Path, *, trust_roots: dict[str, str] | None = None) -> PluginArtifact:
    """Materialize a local path, Git revision, or exact PyPI requirement."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "artifact"
    if source.startswith(("git+", "https://", "ssh://")):
        subprocess.run(["git", "clone", "--depth", "1", source.removeprefix("git+"), str(target)], check=True, capture_output=True, text=True)
        revision = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        artifact = PluginArtifact(source, revision, _digest_tree(target))
        return verify_plugin_artifact(target, trust_roots) if trust_roots is not None else artifact
    candidate = Path(source).expanduser().resolve()
    if candidate.is_dir():
        shutil.copytree(candidate, target)
        artifact = PluginArtifact(source, str(candidate), _digest_tree(target))
        return verify_plugin_artifact(target, trust_roots) if trust_roots is not None else artifact
    if "==" not in source:
        raise ValueError("PyPI plugin sources must pin an exact version: name==version")
    report = destination / "pip-report.json"
    subprocess.run([
        "python", "-m", "pip", "download", "--no-deps", "--dest", str(target),
        "--report", str(report), source,
    ], check=True, capture_output=True, text=True)
    data = json.loads(report.read_text(encoding="utf-8"))
    dependencies = tuple(sorted(item["metadata"]["name"] + "==" + item["metadata"]["version"] for item in data.get("install", [])))
    artifact = PluginArtifact(source, source, _digest_tree(target), dependencies)
    return verify_plugin_artifact(target, trust_roots) if trust_roots is not None else artifact


def install_plugin_artifact(
    name: str,
    source: str,
    installation_root: Path,
    *,
    trust_roots: dict[str, str],
) -> InstalledPluginArtifact:
    """Fetch, verify, and retain one immutable Artifact without activating it.

    Artifact directories are content-addressed so an update cannot mutate the
    files used by a running Plugin host.  The caller records the returned path
    in PluginManager only after this function succeeds.
    """
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("Plugin name must be alphanumeric with optional underscores")
    installation_root.mkdir(parents=True, exist_ok=True)
    staging = installation_root / f".staging-{name}-{uuid.uuid4().hex}"
    try:
        resolved = resolve_plugin_source(source, staging)
        staged_artifact = staging / "artifact"
        verified = verify_plugin_artifact(staged_artifact, trust_roots)
        target = installation_root / name / verified.digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(staging)
        else:
            shutil.move(str(staged_artifact), str(target))
            shutil.rmtree(staging, ignore_errors=True)
        artifact = PluginArtifact(
            source=source,
            resolved=resolved.resolved,
            digest=verified.digest,
            dependencies=verified.dependencies,
            publisher=verified.publisher,
        )
        return InstalledPluginArtifact(artifact, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def update_project_lock(name: str, artifact: PluginArtifact, lock_file: Path = LOCK_FILE) -> dict:
    current = json.loads(lock_file.read_text(encoding="utf-8")) if lock_file.exists() else {"version": 1, "plugins": {}}
    plugins = dict(current.get("plugins", {}))
    plugins[name] = asdict(artifact)
    current = {"version": 1, "plugins": dict(sorted(plugins.items()))}
    write_json_atomic(lock_file, current, sort_keys=True, trailing_newline=True)
    return current
