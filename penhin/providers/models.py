from __future__ import annotations

import os
from dataclasses import dataclass


MODEL_PREFIXES = {
    "anthropic": ("claude-",),
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-"),
    "openai-codex": ("gpt-", "codex-"),
    "gemini": ("gemini-",),
    "deepseek": ("deepseek-",),
}


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    thinking_levels: tuple[str, ...] = ()


# Curated from the same provider catalogs used by Pi. Keep this deliberately
# small enough for a terminal selector; direct model IDs remain supported for
# CLI automation and custom gateways.
MODEL_CATALOG: dict[str, tuple[ModelOption, ...]] = {
    "anthropic": (
        ModelOption("claude-sonnet-5", "Claude Sonnet 5"),
        ModelOption("claude-opus-5", "Claude Opus 5"),
        ModelOption("claude-haiku-4-5", "Claude Haiku 4.5"),
        ModelOption("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ModelOption("claude-opus-4-8", "Claude Opus 4.8"),
    ),
    "openai": (
        ModelOption("gpt-5.6-sol", "GPT-5.6 Sol"),
        ModelOption("gpt-5.6-luna", "GPT-5.6 Luna"),
        ModelOption("gpt-5.6-terra", "GPT-5.6 Terra"),
        ModelOption("gpt-5.5", "GPT-5.5"),
        ModelOption("gpt-5.4", "GPT-5.4"),
        ModelOption("gpt-5.4-mini", "GPT-5.4 mini"),
        ModelOption("gpt-5.3-codex", "GPT-5.3 Codex"),
        ModelOption("o4-mini", "o4-mini"),
    ),
    "openai-codex": (
        ModelOption("gpt-5.6-sol", "GPT-5.6 Sol"),
        ModelOption("gpt-5.6-luna", "GPT-5.6 Luna"),
        ModelOption("gpt-5.6-terra", "GPT-5.6 Terra"),
        ModelOption("gpt-5.5", "GPT-5.5"),
        ModelOption("gpt-5.4", "GPT-5.4"),
        ModelOption("gpt-5.4-mini", "GPT-5.4 mini"),
        ModelOption("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark"),
    ),
    "gemini": (
        ModelOption("gemini-3.6-flash", "Gemini 3.6 Flash"),
        ModelOption("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ModelOption("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite"),
        ModelOption("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
        ModelOption("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite"),
        ModelOption("gemini-3-flash-preview", "Gemini 3 Flash Preview"),
        ModelOption("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ModelOption("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ),
    "deepseek": (
        ModelOption("deepseek-v4-pro", "DeepSeek V4 Pro", ("off", "high", "max")),
        ModelOption("deepseek-v4-flash", "DeepSeek V4 Flash", ("off", "high", "max")),
    ),
}


def model_options(provider: str) -> tuple[ModelOption, ...]:
    return MODEL_CATALOG.get(provider, ())


def model_option(provider: str, model: str) -> ModelOption | None:
    return next((item for item in model_options(provider) if item.id == model), None)


def model_thinking_levels(provider: str, model: str) -> tuple[str, ...]:
    option = model_option(provider, model)
    return option.thinking_levels if option is not None else ()


def parse_model_reference(reference: str, current_provider: str) -> tuple[str, str, str | None]:
    """Resolve Pi-style ``provider/model:thinking`` model references."""
    value = reference.strip()
    if not value:
        raise ValueError("model reference cannot be empty")

    def resolve(candidate: str) -> tuple[str, str] | None:
        if "/" in candidate:
            provider, model = candidate.split("/", 1)
            return (provider, model) if model_option(provider, model) is not None else None
        current = model_option(current_provider, candidate)
        if current is not None:
            return current_provider, current.id
        matches = [
            (provider, item.id)
            for provider, options in MODEL_CATALOG.items()
            for item in options
            if item.id == candidate
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous model {candidate!r}; use provider/model")
        return None

    exact = resolve(value)
    if exact is not None:
        return exact[0], exact[1], None

    base, separator, level = value.rpartition(":")
    resolved = resolve(base) if separator else None
    if resolved is None:
        raise ValueError(f"Unknown model: {value}")
    levels = model_thinking_levels(*resolved)
    if level not in levels:
        choices = ", ".join(levels) or "none"
        raise ValueError(f"Thinking level {level!r} is not supported by {resolved[0]}/{resolved[1]}; choose {choices}")
    return resolved[0], resolved[1], level


def supports_custom_model(provider: str) -> bool:
    skip_check = os.getenv("PENHIN_SKIP_MODEL_COMPATIBILITY_CHECK", "").lower() in {"1", "true", "yes", "on"}
    return bool(
        skip_check
        or (provider == "anthropic" and os.getenv("ANTHROPIC_BASE_URL"))
        or (provider == "openai" and os.getenv("OPENAI_BASE_URL"))
        or (provider == "deepseek" and os.getenv("DEEPSEEK_BASE_URL"))
    )


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
