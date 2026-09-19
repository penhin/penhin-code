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
SHELL_PUNCTUATION = ";|&<>"


def bash_command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return []


def bash_command_operations(command: str) -> list[list[str]] | None:
    """Parse simple shell operations without treating a compound command as one grant.

    Shell substitution and redirection deliberately return ``None``: callers must
    ask for a one-off review rather than infer a reusable prefix from syntax that
    can hide another operation.
    """
    if "\n" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any("`" in token or "$(" in token for token in tokens):
        return None
    operations: list[list[str]] = [[]]
    for token in tokens:
        if token in UNSAFE_SHELL_OPERATORS:
            if not operations[-1]:
                return None
            operations.append([])
        elif token in {"<", ">", "<<", ">>"}:
            return None
        else:
            operations[-1].append(token)
    return operations if operations[-1] else None


def has_shell_operator(command: str) -> bool:
    return any(operator in command for operator in UNSAFE_SHELL_OPERATORS)


def suggest_bash_prefix(command: str) -> str | None:
    operations = bash_command_operations(command)
    if operations is None or len(operations) != 1:
        return None
    tokens = operations[0]

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

    operations = bash_command_operations(command)
    if operations is None or len(operations) != 1:
        return False

    prefix = prefix_rule[:-2].strip()
    normalized_command = " ".join(operations[0])
    return normalized_command == prefix or normalized_command.startswith(prefix + " ")


def approval_rule_key(tool_name: str, rule: str) -> str:
    return f"{tool_name}:{rule}"
