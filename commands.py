import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from prompt_toolkit.completion import Completer, Completion

import ui

from config import CONFIG_FILE, ENV_FILE, get_permission_mode, set_env_value, set_permission_mode
from context import RunContext, conversation_turn_ranges, parse_snip_selectors
from permissions import PERMISSION_MODES, PermissionMode, transition_mode
from runtime import get_runtime, set_runtime_api_key, set_runtime_model
from tool_runtime import runtime_permission_setup
from tools.registry import tool_names
from tools.workspace import workspace_info
from transcript import session_id_from_path


CommandHandler = Callable[[list[str], RunContext | None], None]


@dataclass(frozen=True)
class LocalCommand:
    name: str
    description: str
    handler: CommandHandler
    

def handle_local_command(text: str, context: RunContext | None = None) -> bool:
    if not text.startswith("/"):
        return False
    
    parts = text.split()
    command_name = parts[0]
    args = parts[1:]
    
    command = LOCAL_COMMANDS.get(command_name)
    if command is None:
        ui.print_error(f"Unknown command: {command_name}")
        return True
    
    command.handler(args, context)
    return True

    
def handle_workspace_command(args: list[str], context: RunContext | None = None):
    ui.print_json(workspace_info(tool_names()))


def handle_help_command(args: list[str], context: RunContext | None = None):
    for command in LOCAL_COMMANDS.values():
        ui.print_info(f"{command.name} {command.description}")


def provider_label(provider: str) -> str:
    labels = {
        "anthropic": "Anthropic API",
        "openai": "OpenAI API",
        "gemini": "Gemini API",
    }
    return labels.get(provider, provider or "-")


def provider_base_url(provider: str) -> tuple[str, str] | None:
    env_names = {
        "anthropic": ("ANTHROPIC_BASE_URL", "Anthropic base URL"),
        "openai": ("OPENAI_BASE_URL", "OpenAI base URL"),
    }
    env_name, label = env_names.get(
        provider,
        (f"{provider.upper()}_BASE_URL", f"{provider} base URL"),
    )
    value = os.getenv(env_name, "")
    if not value:
        return None
    return label, value


def proxy_url() -> str:
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or "-"
    )


def setting_sources() -> str:
    sources = []
    if CONFIG_FILE.exists():
        sources.append("User config")
    if ENV_FILE.exists():
        sources.append("User env")
    if Path(".env").exists():
        sources.append("Project env")
    return ", ".join(sources) if sources else "-"


def session_id(context: RunContext | None) -> str:
    if context is None or context.session_path is None:
        return "-"
    return session_id_from_path(context.session_path)


def runtime_model() -> str:
    try:
        return get_runtime().model
    except RuntimeError:
        return os.getenv("MODEL_ID", "-")


def build_status_lines(context: RunContext | None = None) -> list[str]:
    provider = os.getenv("LLM_PROVIDER", "").strip().lower() or "anthropic"
    base_url = provider_base_url(provider)
    lines = [
        f"Version: {os.getenv('PENHIN_VERSION', 'dev')}",
        "Session name: /rename to add a name",
        f"Session ID: {session_id(context)}",
        f"cwd: {workspace_info().get('cwd', '-')}",
        f"API provider: {provider_label(provider)}",
    ]
    if base_url:
        label, value = base_url
        lines.append(f"{label}: {value}")
    lines.extend([
        f"Proxy: {proxy_url()}",
        "",
        f"Model: {runtime_model()}",
        "IDE: Not connected",
        "MCP servers: none",
        f"Permission mode: {get_permission_mode()}",
        f"Setting sources: {setting_sources()}",
    ])
    return lines


def handle_status_command(args: list[str], context: RunContext | None = None):
    for line in build_status_lines(context):
        ui.print_info(line)


def handle_permission_command(args: list[str], context: RunContext | None = None):
    if not args:
        ui.print_info(f"permission: {get_permission_mode()}")
        return

    mode = args[0]
    if mode not in PERMISSION_MODES:
        ui.print_error(f"Unknown permission mode: {mode}")
        ui.print_info(f"Available modes: {', '.join(sorted(PERMISSION_MODES))}")
        return

    if context is not None:
        current = PermissionMode(get_permission_mode())
        target = PermissionMode(mode)
        transition_mode(current, target, context)

    set_permission_mode(mode)
    policy, approval = runtime_permission_setup(mode)
    if context is not None:
        context.policy = policy
        context.approval = approval
    ui.print_info(f"permission: {mode}")


def handle_model_command(args: list[str], context: RunContext | None = None):
    if not args:
        try:
            ui.print_info(f"model: {get_runtime().model}")
        except RuntimeError:
            ui.print_error("Runtime is not initialized.")
        return

    model = " ".join(args).strip()
    if not model:
        ui.print_error("Usage: /model MODEL_ID")
        return

    try:
        set_runtime_model(model)
    except ValueError as error:
        ui.print_error(str(error))
        return
    os.environ["MODEL_ID"] = model
    set_env_value("MODEL_ID", model)
    ui.print_info(f"model: {model}")


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def handle_api_key_command(args: list[str], context: RunContext | None = None):
    provider = os.getenv("LLM_PROVIDER", "").strip().lower() or "anthropic"
    key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}.get(provider)
    if key_name is None:
        ui.print_error(f"Unsupported provider: {provider}")
        return
    if not args:
        value = os.getenv(key_name, "")
        if value:
            ui.print_info(f"api-key: {mask_secret(value)}")
        else:
            ui.print_info("api-key: not set")
        return

    api_key = " ".join(args).strip()
    if not api_key:
        ui.print_error(f"Usage: /api-key {key_name}")
        return

    os.environ[key_name] = api_key
    set_env_value(key_name, api_key)
    set_runtime_api_key(api_key)
    ui.print_info("api-key: saved")


def handle_circuit_command(args: list[str], context: RunContext | None = None):
    try:
        runtime = get_runtime()
    except RuntimeError as error:
        ui.print_error(str(error))
        return

    ui.print_json(
        {
            "main": circuit_status(runtime.circuit_breaker),
            "compact": circuit_status(runtime.compact_circuit_breaker),
        }
    )


def circuit_status(breaker):
    if breaker is None:
        return {"enabled": False}

    status = {"enabled": True}
    status.update(breaker.snapshot())
    return status


def handle_compact_command(args: list[str], context: RunContext | None = None):
    if context is None:
        ui.print_error("No active session to compact.")
        return

    hint = " ".join(args).strip()
    context.force_auto_compact(hint=hint or None)
    if hint:
        ui.print_info("compact: done with hint")
    else:
        ui.print_info("compact: done")


def handle_force_snip_command(args: list[str], context: RunContext | None = None):
    if context is None:
        ui.print_error("No active session to snip.")
        return

    if not args:
        turns = conversation_turn_ranges(context.messages)
        if not turns:
            ui.print_info("snip: no turns")
            return
        for turn_number, start, end, summary in turns:
            ui.print_info(f"{turn_number}: messages {start + 1}-{end} {summary}")
        return

    try:
        selectors = parse_snip_selectors(args)
    except ValueError:
        ui.print_error("Usage: /force-snip [turn|start-end] ...")
        return

    snipped = context.force_snip_turns(selectors)
    ui.print_info(f"snip: marked {snipped} messages")


def complete_local_command(text: str, state: int) -> str | None:
    matches = [
        name for name in LOCAL_COMMANDS
        if name.startswith(text)
    ]

    if state < len(matches):
        return matches[state]
    return None


class LocalCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return

        for name in LOCAL_COMMANDS:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text))


def setup_command_completion() -> LocalCommandCompleter:
    completer = LocalCommandCompleter()
    ui.set_prompt_completer(completer)
    return completer

    
LOCAL_COMMANDS = {
    "/workspace": LocalCommand(
        name="/workspace",
        description="Show workspace summary",
        handler=handle_workspace_command,
    ),
    "/permission": LocalCommand(
        name="/permission",
        description="Show or set permission mode",
        handler=handle_permission_command,
    ),
    "/perm": LocalCommand(
        name="/perm",
        description="Alias for /permission",
        handler=handle_permission_command,
    ),
    "/help": LocalCommand(
        name="/help",
        description="Show local commands",
        handler=handle_help_command,
    ),
    "/status": LocalCommand(
        name="/status",
        description="Show session and runtime status",
        handler=handle_status_command,
    ),
    "/model": LocalCommand(
        name="/model",
        description="Show or set model id",
        handler=handle_model_command,
    ),
    "/api-key": LocalCommand(
        name="/api-key",
        description="Show or set Anthropic API key",
        handler=handle_api_key_command,
    ),
    "/circuit": LocalCommand(
        name="/circuit",
        description="Show circuit breaker status",
        handler=handle_circuit_command,
    ),
    "/compact": LocalCommand(
        name="/compact",
        description="Compact current session, optionally with a hint",
        handler=handle_compact_command,
    ),
    "/force-snip": LocalCommand(
        name="/force-snip",
        description="Mark selected history turns as snipped",
        handler=handle_force_snip_command,
    ),
}
