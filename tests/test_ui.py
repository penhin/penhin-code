import pytest

from penhin.cli import ui


def test_secret_prompt_interruption_does_not_mask_later_input(monkeypatch) -> None:
    calls = []

    class Session:
        def prompt(self, _message, **kwargs):
            calls.append(kwargs.get("is_password"))
            if kwargs.get("is_password"):
                raise KeyboardInterrupt
            return "visible input"

    monkeypatch.setattr(ui, "prompt_session", Session())

    with pytest.raises(KeyboardInterrupt):
        ui.prompt_secret("API key")

    assert ui.prompt_text("Model") == "visible input"
    assert calls == [True, False]


def test_select_accepts_a_unique_search_term(monkeypatch) -> None:
    class Session:
        def prompt(self, _message, **_kwargs):
            return "pro"

    monkeypatch.setattr(ui, "prompt_session", Session())

    assert ui.prompt_select("Model", (
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    )) == "deepseek-v4-pro"


def test_select_rejects_an_ambiguous_search_term(monkeypatch) -> None:
    class Session:
        def prompt(self, _message, **_kwargs):
            return "deepseek"

    monkeypatch.setattr(ui, "prompt_session", Session())

    with pytest.raises(ValueError, match="ambiguous"):
        ui.prompt_select("Model", (
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ))
