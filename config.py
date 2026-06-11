import json
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_text


CONFIG_DIR = Path.home() / ".penhin"
CONFIG_FILE = CONFIG_DIR / "config.json"
ENV_FILE = CONFIG_DIR / ".env"
DEFAULT_CONFIG = {"permission_mode": "default"}


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
    )


def get_permission_mode() -> str:
    return str(load_config().get("permission_mode", DEFAULT_CONFIG["permission_mode"]))


def set_permission_mode(mode: str) -> None:
    config = load_config()
    config["permission_mode"] = mode
    save_config(config)
