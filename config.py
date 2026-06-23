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


def set_env_value(name: str, value: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = []
    if ENV_FILE.exists():
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    replacement = f"{name}={value}"
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key == name:
            new_lines.append(replacement)
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(replacement)

    atomic_write_text(ENV_FILE, "\n".join(new_lines).rstrip() + "\n")


def get_permission_mode() -> str:
    return str(load_config().get("permission_mode", DEFAULT_CONFIG["permission_mode"]))


def set_permission_mode(mode: str) -> None:
    config = load_config()
    config["permission_mode"] = mode
    save_config(config)
