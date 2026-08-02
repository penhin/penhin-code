from __future__ import annotations

from penhin.auth import ResolvedAuth
from penhin.providers.registry import create_provider
from penhin.providers.protocols import LLMProvider

from .models import AuthenticationRequired


def build_provider(provider: str, resolved: ResolvedAuth) -> LLMProvider:
    try:
        return create_provider(provider, resolved)
    except (TypeError, ValueError) as error:
        raise AuthenticationRequired(str(error)) from error
