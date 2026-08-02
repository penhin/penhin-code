import pytest

from penhin.auth import ApiKeyCredential, ResolvedAuth
from penhin.providers.models import model_options, parse_model_reference, validate_model
from penhin.runtime import manager as runtime
from penhin.runtime.factory import build_provider


@pytest.mark.parametrize(("provider", "model"), [
    ("anthropic", "claude-sonnet-5"),
    ("openai", "gpt-5.6"),
    ("openai", "o3-mini"),
    ("gemini", "gemini-3.5-flash"),
    ("deepseek", "deepseek-v4-pro"),
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


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openai-codex", "gemini", "deepseek"])
def test_model_catalog_contains_only_compatible_unique_models(monkeypatch, provider: str) -> None:
    monkeypatch.delenv("PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    options = model_options(provider)
    assert len(options) >= (2 if provider == "deepseek" else 5)
    assert len({item.id for item in options}) == len(options)
    for item in options:
        validate_model(provider, item.id)


def test_parse_pi_style_model_reference_with_thinking_level() -> None:
    assert parse_model_reference("deepseek/deepseek-v4-pro:max", "anthropic") == (
        "deepseek", "deepseek-v4-pro", "max",
    )
    assert parse_model_reference("deepseek-v4-flash", "deepseek") == (
        "deepseek", "deepseek-v4-flash", None,
    )


def test_parse_model_reference_rejects_unsupported_thinking_level() -> None:
    with pytest.raises(ValueError, match="not supported"):
        parse_model_reference("deepseek/deepseek-v4-pro:medium", "anthropic")


def test_build_provider_selects_openai(monkeypatch) -> None:
    expected = object()
    resolved = ResolvedAuth("openai", ApiKeyCredential(key="test-key"), "test")
    from unittest.mock import patch
    with patch("penhin.providers.openai.OpenAIProvider", return_value=expected):
        assert build_provider("openai", resolved) is expected


def test_build_provider_selects_deepseek() -> None:
    expected = object()
    resolved = ResolvedAuth("deepseek", ApiKeyCredential(key="test-key"), "test")
    from unittest.mock import patch
    with patch("penhin.providers.deepseek.DeepSeekProvider", return_value=expected) as provider:
        assert build_provider("deepseek", resolved) is expected
    provider.assert_called_once_with(api_key="test-key", base_url="https://api.deepseek.com")


def test_optional_runtime_start_allows_login_without_credentials(monkeypatch) -> None:
    runtime.runtime = object()
    monkeypatch.setattr(runtime, "load_environment", lambda: None)
    monkeypatch.setattr(runtime, "configured_provider", lambda: "anthropic")
    monkeypatch.setattr(runtime, "resolve_runtime_auth", lambda _provider: None)
    runtime.init_runtime(required=False)
    assert runtime.runtime is None


def test_required_runtime_start_rejects_missing_credentials(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "load_environment", lambda: None)
    monkeypatch.setattr(runtime, "configured_provider", lambda: "anthropic")
    monkeypatch.setattr(runtime, "resolve_runtime_auth", lambda _provider: None)
    with pytest.raises(SystemExit):
        runtime.init_runtime(required=True)
