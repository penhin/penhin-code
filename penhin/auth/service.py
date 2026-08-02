from __future__ import annotations

from collections.abc import Callable

from .models import Credential, ResolvedAuth
from .resolver import AuthResolver, auth_resolver
from .storage import CredentialStore, credential_store


class AuthService:
    """Application boundary for credential lookup and persistent mutation."""

    def __init__(self, resolver: AuthResolver | None = None, store: CredentialStore | None = None) -> None:
        self._store = store
        self._resolver = resolver

    @property
    def store(self) -> CredentialStore:
        return self._store or credential_store()

    @property
    def resolver(self) -> AuthResolver:
        return self._resolver or auth_resolver(self._store)

    def resolve(self, provider: str) -> ResolvedAuth | None:
        return self.resolver.resolve(provider)

    def status(self, provider: str) -> dict[str, object]:
        return self.resolver.status(provider)

    def save(self, provider: str, credential: Credential) -> Credential:
        return self.store.modify(provider, lambda _current: credential)

    def modify(self, provider: str, update: Callable[[Credential | None], Credential | None]) -> Credential | None:
        return self.store.modify(provider, update)

    def logout(self, provider: str) -> None:
        self.store.delete(provider)


auth_service = AuthService()


__all__ = ["AuthService", "auth_service"]
