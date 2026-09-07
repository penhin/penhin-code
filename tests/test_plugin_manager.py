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
