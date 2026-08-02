from __future__ import annotations

import os


MODEL_PREFIXES = {
    "anthropic": ("claude-",),
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-"),
    "openai-codex": ("gpt-", "codex-"),
    "gemini": ("gemini-",),
}


def validate_model(provider: str, model: str) -> None:
    if os.getenv("PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK", "").lower() in {"1", "true", "yes", "on"}:
        return
    if provider == "anthropic" and os.getenv("ANTHROPIC_BASE_URL"):
        return
    if provider == "openai" and os.getenv("OPENAI_BASE_URL"):
        return
    prefixes = MODEL_PREFIXES.get(provider)
    if prefixes is None or not model.startswith(prefixes):
        expected = ", ".join(prefixes or ())
        raise ValueError(f"Model {model!r} is not compatible with provider {provider!r}; expected prefix: {expected}")
