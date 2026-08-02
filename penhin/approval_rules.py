from __future__ import annotations

import shlex


SAFE_SINGLE_WORD_PREFIXES = {
    "grep",
    "ls",
    "pwd",
    "pytest",
    "rg",
}

SAFE_TWO_WORD_PREFIXES = {
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "status"),
    ("python", "-m"),
    ("python3", "-m"),
}

UNSAFE_SHELL_OPERATORS = {"&&", "||", ";", "|", "&"}


def bash_command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return []


def has_shell_operator(command: str) -> bool:
    return any(operator in command for operator in UNSAFE_SHELL_OPERATORS)


def suggest_bash_prefix(command: str) -> str | None:
    if "\n" in command or has_shell_operator(command):
        return None

    tokens = bash_command_tokens(command)
    if not tokens:
        return None

    if len(tokens) >= 2 and tuple(tokens[:2]) in SAFE_TWO_WORD_PREFIXES:
        if tokens[:2] in (["python", "-m"], ["python3", "-m"]):
            if len(tokens) >= 3 and tokens[2] == "pytest":
                return " ".join(tokens[:3]) + ":*"
            return None
        return " ".join(tokens[:2]) + ":*"

    if tokens[0] in SAFE_SINGLE_WORD_PREFIXES:
        return tokens[0] + ":*"

    return None


def bash_prefix_matches(command: str, prefix_rule: str) -> bool:
    if not prefix_rule.endswith(":*"):
        return command.strip() == prefix_rule.strip()

    if "\n" in command or has_shell_operator(command):
        return False

    prefix = prefix_rule[:-2].strip()
    command = command.strip()
    return command == prefix or command.startswith(prefix + " ")


def approval_rule_key(tool_name: str, rule: str) -> str:
    return f"{tool_name}:{rule}"
