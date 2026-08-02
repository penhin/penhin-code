import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from penhin.infrastructure.atomic_io import atomic_write_text


CONFIG_DIR = Path.home() / ".penhin"
CONFIG_FILE = CONFIG_DIR / "config.json"
ENV_FILE = CONFIG_DIR / ".env"
DEFAULT_CONFIG = {"permission_mode": "default"}
PACKAGE_NAME = "penhin-code"


def get_version() -> str:
    override = os.getenv("PENHIN_VERSION", "").strip()
    if override:
        return override
    try:
        installed = version(PACKAGE_NAME)
        return installed or "dev"
    except PackageNotFoundError:
        return "dev"


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)

    config = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        config.update(data)
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        CONFIG_FILE,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


def get_permission_mode() -> str:
    return str(load_config().get("permission_mode", DEFAULT_CONFIG["permission_mode"]))


def set_permission_mode(mode: str) -> None:
    config = load_config()
    config["permission_mode"] = mode
    save_config(config)


def get_credential_backend() -> str:
    return str(load_config().get("credential_backend", "keyring"))


def set_credential_backend(backend: str) -> None:
    if backend not in {"keyring", "file"}:
        raise ValueError(f"unsupported credential backend: {backend}")
    config = load_config()
    config["credential_backend"] = backend
    save_config(config)


def get_provider_model(provider: str) -> str:
    models = load_config().get("provider_models", {})
    return str(models.get(provider, "")) if isinstance(models, dict) else ""


def set_provider_model(provider: str, model: str) -> None:
    config = load_config()
    models = config.get("provider_models", {})
    if not isinstance(models, dict):
        models = {}
    config["provider_models"] = {**models, provider: model}
    save_config(config)


def get_provider_thinking_level(provider: str) -> str:
    levels = load_config().get("provider_thinking_levels", {})
    return str(levels.get(provider, "")) if isinstance(levels, dict) else ""


def set_provider_thinking_level(provider: str, level: str) -> None:
    config = load_config()
    levels = config.get("provider_thinking_levels", {})
    if not isinstance(levels, dict):
        levels = {}
    config["provider_thinking_levels"] = {**levels, provider: level}
    save_config(config)


def get_active_provider() -> str:
    return str(load_config().get("active_provider", "anthropic"))


def set_active_provider(provider: str) -> None:
    config = load_config()
    config["active_provider"] = provider
    save_config(config)
