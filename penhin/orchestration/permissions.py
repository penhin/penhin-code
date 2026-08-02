from __future__ import annotations

import os
import shlex


READONLY_MODE = "readonly"
READONLY_COMMANDS = {"cat", "find", "git", "grep", "head", "ls", "pwd", "pytest", "rg", "sed", "tail", "wc"}
READONLY_GIT_SUBCOMMANDS = {"diff", "grep", "log", "show", "status"}


def workspace_mode() -> str:
    return os.getenv("PENHIN_WORKSPACE_MODE", "shared")


def write_is_allowed() -> bool:
    return workspace_mode() != READONLY_MODE


def readonly_command_is_allowed(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or any(operator in command for operator in (";", "&&", "||", ">", "<", "|", "`", "$(`")):
        return False
    executable = tokens[0]
    if executable in READONLY_COMMANDS - {"git"}:
        return True
    if executable in {"python", "python3", ".venv/bin/python"}:
        return len(tokens) >= 3 and tokens[1:3] == ["-m", "pytest"]
    if executable == "git" and len(tokens) >= 2:
        return tokens[1] in READONLY_GIT_SUBCOMMANDS
    return False
