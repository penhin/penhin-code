import re
import shlex
import subprocess
from pathlib import Path

from penhin.result import Result
from penhin.orchestration.permissions import readonly_command_is_allowed, write_is_allowed

from .workspace import IGNORED_PATH_PARTS, WORKDIR


DANGEROUS_COMMAND_NAMES = {"sudo", "shutdown", "reboot"}
DANGEROUS_RM_ROOT = re.compile(r"(^|[;&|]\s*)rm\s+(-[A-Za-z]*[rf][A-Za-z]*\s+)+/(\s|$)")
PARENT_TRAVERSAL = re.compile(r"(^|[\s/'\"=])\.\.($|[\s/'\";&|])")
SENSITIVE_CREDENTIAL_COMMAND = re.compile(
    r"(?:\bkeyring\b|\bsecret-tool\b|find-generic-password|\bcmdkey\b|credentialmanager|auth\.json|penhin-code)",
    re.IGNORECASE,
)


def command_references_ignored_path(command: str) -> str | None:
    if SENSITIVE_CREDENTIAL_COMMAND.search(command):
        return "credential access"
    environment_file = re.search(r"(^|[\s/\"'`=])\.env(?:\.[A-Za-z0-9_-]+)?($|[\s/\"'`/*])", command)
    if environment_file:
        return environment_file.group(0).strip() or ".env"
    for part in IGNORED_PATH_PARTS:
        pattern = rf"(^|[\s/\"'`=]){re.escape(part)}($|[\s/\"'`/*])"
        if re.search(pattern, command):
            return part
    return None


def command_uses_dangerous_name(command: str) -> str | None:
    for name in DANGEROUS_COMMAND_NAMES:
        pattern = rf"(^|[;&|]\s*){re.escape(name)}(\s|$)"
        if re.search(pattern, command):
            return name
    return None


def command_is_dangerous(command: str) -> str | None:
    dangerous_name = command_uses_dangerous_name(command)
    if dangerous_name:
        return dangerous_name
    if DANGEROUS_RM_ROOT.search(command):
        return "rm"
    return None


def command_escapes_workspace(command: str) -> str | None:
    """Reject shell paths that can leave the assigned agent worktree."""
    if PARENT_TRAVERSAL.search(command):
        return "parent traversal"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "invalid shell syntax"
    for token in tokens[1:]:
        candidate = token.split("=", 1)[-1].rstrip(",;:)")
        if Path(candidate).is_absolute():
            resolved = Path(candidate).resolve()
            if not resolved.is_relative_to(WORKDIR):
                return "absolute path outside workspace"
    return None


def run_bash(command: str) -> Result:
    if not write_is_allowed() and not readonly_command_is_allowed(command):
        return Result.failure(
            "Error: command is not allowed in a readonly agent worktree",
            code="readonly_workspace",
        )
    dangerous_command = command_is_dangerous(command)
    if dangerous_command:
        return Result.failure(
            f"Error: blocked dangerous command: {dangerous_command}",
            code="blocked_command",
            command=dangerous_command,
        )
    escape = command_escapes_workspace(command)
    if escape:
        return Result.failure(
            f"Error: command may escape the agent worktree: {escape}",
            code="workspace_escape",
            reason=escape,
        )
    ignored_part = command_references_ignored_path(command)
    if ignored_part:
        return Result.failure(
            f"Error: command references ignored path: {ignored_part}",
            code="ignored_path",
            ignored_part=ignored_part,
        )
    try:
        from penhin.auth.secrets import scrubbed_environment
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=30,
            env=scrubbed_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result.failure("Error: Timeout (30s)", code="timeout", timeout_seconds=30)
    except OSError as error:
        return Result.failure(f"Error: {error}", code="os_error")
    from penhin.auth.secrets import redact_text
    stdout = redact_text(result.stdout)
    stderr = redact_text(result.stderr)
    return Result(
        ok=result.returncode == 0,
        message=stdout,
        error=stderr,
        data={
            "command": redact_text(command),
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        },
    )
