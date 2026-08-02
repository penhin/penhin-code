from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass


_lock = threading.Lock()
_secret_values: set[str] = set()
_sensitive_name = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|authorization[_-]?code|device[_-]?code|code[_-]?verifier|auth[_-]?url|credential|password|secret)$",
    re.IGNORECASE,
)
_explicit_sensitive_env = {
    "DATABASE_URL", "PENHIN_DATABASE_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AZURE_CLIENT_SECRET", "GOOGLE_APPLICATION_CREDENTIALS",
}


def sensitive_name(name: object) -> bool:
    return bool(_sensitive_name.search(str(name)))


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        with _lock:
            _secret_values.add(value)


def registered_secrets() -> tuple[str, ...]:
    with _lock:
        return tuple(sorted(_secret_values, key=len, reverse=True))


def redact_text(value: str) -> str:
    result = value
    candidates = list(registered_secrets())
    for name, secret in os.environ.items():
        if sensitive_name(name) and secret and len(secret) >= 6:
            candidates.append(secret)
    for secret in sorted(set(candidates), key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    return result


def safe_value(value, *, max_string_chars: int | None = None):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if sensitive_name(key) else safe_value(item, max_string_chars=max_string_chars)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_value(item, max_string_chars=max_string_chars) for item in value]
    if isinstance(value, str):
        redacted = redact_text(value)
        if max_string_chars is not None and len(redacted) > max_string_chars:
            return redacted[:max_string_chars] + f"...<truncated:{len(redacted) - max_string_chars}>"
        return redacted
    return value


def scrubbed_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in list(environment):
        upper = name.upper()
        if upper in _explicit_sensitive_env or sensitive_name(name) or upper.endswith(("_TOKEN", "_PASSWORD", "_SECRET", "_DATABASE_URL")):
            environment.pop(name, None)
    return environment


def trusted_worker_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if source is None else source
    exact = {
        "PATH", "PYTHONPATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ",
        "LLM_PROVIDER", "MODEL_ID", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
        "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        "https_proxy", "http_proxy", "no_proxy", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    }
    return {
        name: value for name, value in values.items()
        if name in exact or (name.startswith("PENHIN_") and not sensitive_name(name))
    }
