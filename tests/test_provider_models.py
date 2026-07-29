from unittest.mock import patch

import pytest

from providers.models import validate_model
import runtime


@pytest.mark.parametrize(("provider", "model"), [
    ("anthropic", "claude-sonnet-5"),
    ("openai", "gpt-5.6"),
    ("openai", "o3-mini"),
    ("gemini", "gemini-3.5-flash"),
])
def test_validate_model_accepts_native_provider_models(monkeypatch, provider: str, model: str) -> None:
    monkeypatch.delenv("PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK", raising=False)
    validate_model(provider, model)


def test_validate_model_rejects_an_incompatible_native_model(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="not compatible"):
        validate_model("openai", "claude-sonnet-5")


def test_validate_model_allows_a_custom_openai_gateway(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    validate_model("openai", "custom-model")


def test_build_provider_selects_openai_and_rejects_unknown_provider(monkeypatch) -> None:
    expected = object()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with patch("providers.openai.OpenAIProvider.from_env", return_value=expected):
        assert runtime.build_provider_from_env() is expected

    monkeypatch.setenv("LLM_PROVIDER", "unsupported")
    with pytest.raises(ValueError, match="Unsupported"):
        runtime.build_provider_from_env()
