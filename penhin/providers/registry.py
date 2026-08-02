from __future__ import annotations

import os
from collections.abc import Callable

from penhin.auth import ApiKeyCredential, OAuthCredential, ResolvedAuth, provider_auth

from .protocols import LLMProvider


ProviderBuilder = Callable[[ResolvedAuth], LLMProvider]


def _anthropic(resolved: ResolvedAuth) -> LLMProvider:
    from .anthropic import AnthropicProvider

    credential = provider_auth("anthropic").resolve(resolved.credential)
    if isinstance(credential, OAuthCredential):
        return AnthropicProvider(
            auth_token=credential.access_token,
            oauth=True,
            base_url=os.getenv("ANTHROPIC_BASE_URL"),
        )
    if isinstance(credential, ApiKeyCredential):
        return AnthropicProvider(api_key=credential.key, base_url=os.getenv("ANTHROPIC_BASE_URL"))
    raise TypeError("anthropic requires an API key or OAuth credential")


def _openai(resolved: ResolvedAuth) -> LLMProvider:
    from .openai import OpenAIProvider

    credential = provider_auth("openai").resolve(resolved.credential)
    if not isinstance(credential, ApiKeyCredential):
        raise TypeError("openai requires an API key credential")
    return OpenAIProvider(api_key=credential.key, base_url=os.getenv("OPENAI_BASE_URL"))


def _gemini(resolved: ResolvedAuth) -> LLMProvider:
    from .gemini import GeminiProvider

    credential = provider_auth("gemini").resolve(resolved.credential)
    if not isinstance(credential, ApiKeyCredential):
        raise TypeError("gemini requires an API key credential")
    return GeminiProvider(api_key=credential.key)


def _deepseek(resolved: ResolvedAuth) -> LLMProvider:
    from .deepseek import DeepSeekProvider

    credential = provider_auth("deepseek").resolve(resolved.credential)
    if not isinstance(credential, ApiKeyCredential):
        raise TypeError("deepseek requires an API key credential")
    return DeepSeekProvider(api_key=credential.key, base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))


def _openai_codex(resolved: ResolvedAuth) -> LLMProvider:
    from .openai_codex import OpenAICodexProvider

    credential = provider_auth("openai-codex").resolve(resolved.credential)
    if not isinstance(credential, OAuthCredential):
        raise TypeError("openai-codex requires an OAuth credential")
    if not credential.account_id:
        raise ValueError("OpenAI Codex credential has no ChatGPT account id")
    return OpenAICodexProvider(credential.access_token, credential.account_id)


_BUILDERS: dict[str, ProviderBuilder] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "gemini": _gemini,
    "deepseek": _deepseek,
    "openai-codex": _openai_codex,
}


def provider_ids() -> tuple[str, ...]:
    return tuple(_BUILDERS)


def create_provider(provider: str, resolved: ResolvedAuth) -> LLMProvider:
    try:
        builder = _BUILDERS[provider]
    except KeyError as error:
        raise ValueError(f"Unsupported provider: {provider}") from error
    return builder(resolved)


__all__ = ["create_provider", "provider_ids"]
