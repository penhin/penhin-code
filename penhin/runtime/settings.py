from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from penhin.auth.resolver import set_process_environment_names
from penhin.infrastructure.config import ENV_FILE

from .retry import CircuitBreaker


logger = logging.getLogger("penhin.runtime.settings")
_environment_sources: dict[str, str] = {}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"[config] invalid {name}={value!r}; using {default}")
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[config] invalid {name}={value!r}; using {default}")
        return default


def build_circuit_breaker() -> CircuitBreaker | None:
    if not _env_bool("CIRCUIT_BREAKER_ENABLED", True):
        return None
    return CircuitBreaker(
        failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
        recovery_timeout=_env_float("CIRCUIT_BREAKER_RECOVERY_TIMEOUT", 30.0),
        success_threshold=_env_int("CIRCUIT_BREAKER_SUCCESS_THRESHOLD", 1),
    )


def build_compact_circuit_breaker() -> CircuitBreaker | None:
    if not _env_bool("COMPACT_CIRCUIT_BREAKER_ENABLED", True):
        return None
    return CircuitBreaker(
        failure_threshold=_env_int("COMPACT_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2),
        recovery_timeout=_env_float("COMPACT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT", 60.0),
        success_threshold=_env_int("COMPACT_CIRCUIT_BREAKER_SUCCESS_THRESHOLD", 1),
    )


def configured_provider() -> str:
    return os.getenv("LLM_PROVIDER", "").strip().lower() or "anthropic"


def setting_source(name: str) -> str:
    if not os.getenv(name):
        return "not set"
    return _environment_sources.get(name, "Process environment")


def mark_setting_source(name: str, source: str) -> None:
    _environment_sources[name] = source


def load_environment() -> None:
    _environment_sources.clear()
    original_names = set(os.environ)
    set_process_environment_names(original_names)
    for path, label in ((ENV_FILE, "User env"), (Path(".env"), "Project env")):
        values = dotenv_values(path)
        load_dotenv(path, override=False)
        for name, value in values.items():
            if name not in original_names and value is not None and name not in _environment_sources:
                _environment_sources[name] = label
    for name in original_names:
        _environment_sources[name] = "Process environment"
