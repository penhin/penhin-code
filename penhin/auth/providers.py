from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .interaction import AuthInteraction
from .models import ApiKeyCredential, Credential, OAuthCredential
from .oauth import login_anthropic, login_openai_codex, refresh_oauth
from .secrets import register_secret


class ProviderAuth(Protocol):
    provider_id: str

    def methods(self) -> tuple[str, ...]: ...
    def login(self, auth_type: str, interaction: AuthInteraction, *, oauth_method: str = "browser") -> Credential: ...
    def refresh(self, credential: OAuthCredential) -> OAuthCredential: ...
    def resolve(self, credential: Credential) -> Credential: ...


@dataclass(frozen=True)
class BuiltinProviderAuth:
    provider_id: str
    api_key_env: str | None = None
    oauth: bool = False

    def methods(self) -> tuple[str, ...]:
        return (("api_key",) if self.api_key_env else ()) + (("oauth",) if self.oauth else ())

    def login(self, auth_type: str, interaction: AuthInteraction, *, oauth_method: str = "browser") -> Credential:
        if auth_type == "api_key" and self.api_key_env:
            key = interaction.prompt("secret", f"Enter {self.provider_id} API key")
            if not key:
                raise ValueError("API key cannot be empty")
            register_secret(key)
            return ApiKeyCredential(key=key)
        if auth_type == "oauth" and self.oauth:
            if self.provider_id == "anthropic":
                return login_anthropic(interaction)
            if self.provider_id == "openai-codex":
                return login_openai_codex(interaction, oauth_method)
        raise ValueError(f"{self.provider_id} does not support {auth_type} login")

    def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        if not self.oauth:
            raise ValueError(f"{self.provider_id} does not support OAuth refresh")
        return refresh_oauth(self.provider_id, credential)

    def resolve(self, credential: Credential) -> Credential:
        if isinstance(credential, ApiKeyCredential) and self.api_key_env:
            return credential
        if isinstance(credential, OAuthCredential) and self.oauth:
            return credential
        raise ValueError(f"credential type {credential.type} is not valid for {self.provider_id}")


PROVIDER_AUTHS: dict[str, BuiltinProviderAuth] = {
    "anthropic": BuiltinProviderAuth("anthropic", api_key_env="ANTHROPIC_API_KEY", oauth=True),
    "openai": BuiltinProviderAuth("openai", api_key_env="OPENAI_API_KEY"),
    "openai-codex": BuiltinProviderAuth("openai-codex", oauth=True),
    "gemini": BuiltinProviderAuth("gemini", api_key_env="GEMINI_API_KEY"),
}


def provider_auth_ids() -> tuple[str, ...]:
    return tuple(PROVIDER_AUTHS)


def provider_auth(provider: str) -> BuiltinProviderAuth:
    try:
        return PROVIDER_AUTHS[provider]
    except KeyError as error:
        raise ValueError(f"unsupported auth provider: {provider}") from error


def provider_key_name(provider: str) -> str | None:
    return provider_auth(provider).api_key_env
