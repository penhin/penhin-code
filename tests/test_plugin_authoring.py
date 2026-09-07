from pathlib import Path
import time
from penhin.plugins import authoring


def test_authoring_init_check_and_pack_are_ci_safe(tmp_path: Path) -> None:
    plugin = tmp_path / "demo"
    authoring.init(plugin, "demo")
    assert authoring.check(plugin) == []
    assert authoring.pack(plugin, tmp_path / "demo-plugin").is_file()


def test_authoring_reports_duplicate_contributions(tmp_path: Path) -> None:
    authoring.init(tmp_path / "demo", "demo")
    manifest = tmp_path / "demo" / "penhin-plugin.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n  - name: hello\n    description: duplicate\n    entrypoint: plugin:hello\n", encoding="utf-8")
    assert "duplicate contribution" in authoring.check(tmp_path / "demo")


def test_development_mocks_and_reload_detection_are_safe(tmp_path: Path) -> None:
    authoring.init(tmp_path / "demo", "demo")
    services = authoring.DevelopmentServices()
    assert services.fetch("https://example.test")["body"] == "<mock>"
    before = time.time(); time.sleep(0.01)
    (tmp_path / "demo" / "plugin.py").touch()
    assert authoring.dev_changed(tmp_path / "demo", before)
