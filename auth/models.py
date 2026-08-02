from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias


SUPPORTED_AUTH_PROVIDERS = frozenset({"anthropic", "openai", "openai-codex", "gemini"})


@dataclass(frozen=True)
class ApiKeyCredential:
    type: Literal["api_key"] = "api_key"
    key: str = ""


@dataclass(frozen=True)
class OAuthCredential:
    access_token: str
    refresh_token: str
    expires_at: int
    account_id: str | None = None
    scope: str | None = None
    type: Literal["oauth"] = "oauth"


Credential: TypeAlias = ApiKeyCredential | OAuthCredential


@dataclass(frozen=True)
class ResolvedAuth:
    provider: str
    credential: Credential
    source: str
    backend: str | None = None


def credential_to_dict(credential: Credential) -> dict[str, object]:
    return asdict(credential)


def credential_from_dict(value: object) -> Credential:
    if not isinstance(value, dict):
        raise ValueError("credential must be an object")
    kind = value.get("type")
    if kind == "api_key":
        if set(value) != {"type", "key"} or not isinstance(value.get("key"), str) or not value["key"]:
            raise ValueError("invalid api_key credential")
        return ApiKeyCredential(key=value["key"])
    if kind == "oauth":
        allowed = {"type", "access_token", "refresh_token", "expires_at", "account_id", "scope"}
        if set(value) - allowed:
            raise ValueError("invalid oauth credential fields")
        if not isinstance(value.get("access_token"), str) or not value["access_token"]:
            raise ValueError("invalid oauth access token")
        if not isinstance(value.get("refresh_token"), str) or not value["refresh_token"]:
            raise ValueError("invalid oauth refresh token")
        if not isinstance(value.get("expires_at"), int) or isinstance(value["expires_at"], bool):
            raise ValueError("invalid oauth expiry")
        for optional in ("account_id", "scope"):
            if value.get(optional) is not None and not isinstance(value[optional], str):
                raise ValueError(f"invalid oauth {optional}")
        return OAuthCredential(
            access_token=value["access_token"],
            refresh_token=value["refresh_token"],
            expires_at=value["expires_at"],
            account_id=value.get("account_id"),
            scope=value.get("scope"),
        )
    raise ValueError("unknown credential type")
