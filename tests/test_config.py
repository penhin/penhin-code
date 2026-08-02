from penhin.infrastructure import config


def test_provider_and_model_selection_persist_in_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")

    assert config.get_active_provider() == "anthropic"
    assert config.get_provider_model("gemini") == ""
    assert config.get_provider_thinking_level("gemini") == ""

    config.set_active_provider("gemini")
    config.set_provider_model("gemini", "gemini-3.5-flash")
    config.set_provider_thinking_level("gemini", "high")

    assert config.get_active_provider() == "gemini"
    assert config.get_provider_model("gemini") == "gemini-3.5-flash"
    assert config.get_provider_thinking_level("gemini") == "high"
    text = config.CONFIG_FILE.read_text(encoding="utf-8")
    assert "API_KEY" not in text
    assert "credential" not in text
