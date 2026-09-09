from pathlib import Path
from penhin.plugins.manager import PluginManager


def test_install_auto_enables_and_project_takes_precedence(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project" / "plugins.json")
    manager.install("demo", "global-source", "global")
    manager.install("demo", "project-source")
    assert manager.effective()["demo"]["source"] == "project-source"
    assert manager.effective()["demo"]["enabled"] is True


def test_management_updates_are_persistent(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project.json")
    manager.install("demo", "source")
    manager.set_enabled("demo", False)
    manager.configure("demo", {"language": "zh"})
    assert manager.effective()["demo"] == {"source": "source", "enabled": False, "config": {"language": "zh"}}
    manager.update("demo", "next")
    manager.remove("demo")
    assert "demo" not in manager.effective()


def test_project_trust_and_artifact_authorization_are_scoped_and_only_inherit_compatible_updates(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project.json")
    manager.add_project_trust_root("acme", "public-root")
    manager.authorize_artifact("demo", "acme/demo", "digest-one", ["state"])

    assert manager.project_trust_roots() == {"acme": "public-root"}
    assert manager.authorization_for("demo", "acme/demo", "digest-one", ["state"]) == "approved"
    assert manager.authorization_for("demo", "acme/demo", "digest-two", []) == "inherited"
    assert manager.authorization_for("demo", "acme/demo", "digest-two", ["state", "network"]) == "pending"
    assert manager.authorization_for("demo", "other/demo", "digest-one", ["state"]) == "pending"

    manager.revoke_artifact("demo")
    assert manager.authorization_for("demo", "acme/demo", "digest-one", ["state"]) == "denied"


def test_required_contribution_policy_is_project_scoped(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project.json")
    manager.require_contribution({"plugin": "speech", "id": "transcribe"})
    assert manager.required_contributions() == [{"plugin": "speech", "id": "transcribe"}]


def test_eligible_contribution_is_discovered_from_project_plugin_policy(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "global.json", tmp_path / "project.json")
    manager.install("speech", "source")
    manager.configure("speech", {"contributions": [{"id": "transcribe", "required_eligible": True}]})
    assert manager.eligible_contributions("transcribe") == [{"plugin": "speech", "id": "transcribe"}]
