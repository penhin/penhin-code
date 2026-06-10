import re
import subprocess

from result import Result

from .workspace import IGNORED_PATH_PARTS, WORKDIR


DANGEROUS_COMMAND_NAMES = {"sudo", "shutdown", "reboot"}
DANGEROUS_RM_ROOT = re.compile(r"(^|[;&|]\s*)rm\s+(-[A-Za-z]*[rf][A-Za-z]*\s+)+/(\s|$)")


def command_references_ignored_path(command: str) -> str | None:
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


def run_bash(command: str) -> Result:
    dangerous_command = command_is_dangerous(command)
    if dangerous_command:
        return Result.failure(
            f"Error: blocked dangerous command: {dangerous_command}",
            code="blocked_command",
            command=dangerous_command,
        )
    ignored_part = command_references_ignored_path(command)
    if ignored_part:
        return Result.failure(
            f"Error: command references ignored path: {ignored_part}",
            code="ignored_path",
            ignored_part=ignored_part,
        )
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return Result.failure("Error: Timeout (30s)", code="timeout", timeout_seconds=30)
    except OSError as error:
        return Result.failure(f"Error: {error}", code="os_error")
    return Result(
        ok=result.returncode == 0,
        message=result.stdout,
        error=result.stderr,
        data={
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
