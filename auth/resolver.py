from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from config import ENV_FILE
from .models import ApiKeyCredential, ResolvedAuth
from .providers import provider_key_name
from .storage import CredentialStore, CredentialStoreUnavailable, credential_store


_process_environment_names: set[str] | None = None


def set_process_environment_names(names: set[str]) -> None:
    global _process_environment_names
    _process_environment_names = set(names)


class AuthResolver:
    def __init__(self, store: CredentialStore | None = None):
        self._store = store

    def _credential_store(self) -> CredentialStore:
        return self._store or credential_store()

    def resolve(self, provider: str) -> ResolvedAuth | None:
        key_name = provider_key_name(provider)
        process_value = os.environ.get(key_name, "") if key_name else ""
        source = _environment_source(key_name) if key_name else None
        if process_value and source == "Process environment":
            return ResolvedAuth(provider, ApiKeyCredential(key=process_value), source)

        try:
            store = self._credential_store()
            stored = store.read(provider)
        except CredentialStoreUnavailable:
            if process_value:
                return ResolvedAuth(provider, ApiKeyCredential(key=process_value), source or "Environment")
            raise
        if stored is not None:
            return ResolvedAuth(provider, stored, "Stored credential", store.backend_name)

        if process_value:
            return ResolvedAuth(provider, ApiKeyCredential(key=process_value), source or "Environment")
        return None

    def status(self, provider: str) -> dict[str, object]:
        resolved = self.resolve(provider)
        if resolved is None:
            return {"provider": provider, "configured": False, "type": None, "source": "not set", "backend": None, "expired": None}
        expires_at = getattr(resolved.credential, "expires_at", None)
        import time
        return {
            "provider": provider,
            "configured": True,
            "type": resolved.credential.type,
            "source": resolved.source,
            "backend": resolved.backend,
            "expired": bool(expires_at is not None and expires_at <= int(time.time())),
        }


def _environment_source(name: str | None) -> str | None:
    if not name or not os.getenv(name):
        return None
    if _process_environment_names is None or name in _process_environment_names:
        return "Process environment"
    user_values = dotenv_values(ENV_FILE)
    project_values = dotenv_values(Path(".env"))
    if name in user_values and os.getenv(name) == user_values.get(name):
        return "User env"
    if name in project_values and os.getenv(name) == project_values.get(name):
        return "Project env"
    return "Process environment"


def auth_resolver(store: CredentialStore | None = None) -> AuthResolver:
    return AuthResolver(store)
