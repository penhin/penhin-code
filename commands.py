import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from prompt_toolkit.completion import Completer, Completion

import ui

from config import CONFIG_FILE, ENV_FILE, get_permission_mode, set_env_value, set_permission_mode
from context import RunContext, conversation_turn_ranges, parse_snip_selectors
from permissions import PERMISSION_MODES, PermissionMode, transition_mode
from runtime import (
    configured_provider,
    get_runtime,
    mark_setting_source,
    provider_key_name,
    set_runtime_api_key,
    set_runtime_model,
    set_runtime_provider,
    setting_source,
)
from providers.models import validate_model
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
    provider = configured_provider()
    key_name = provider_key_name(provider)
    base_url = provider_base_url(provider)
    lines = [
        f"Version: {os.getenv('PENHIN_VERSION', 'dev')}",
        "Session name: /rename to add a name",
        f"Session ID: {session_id(context)}",
        f"cwd: {workspace_info().get('cwd', '-')}",
        f"API provider: {provider_label(provider)}",
        (
            f"API key: configured ({key_name}; {setting_source(key_name)})"
            if key_name and os.getenv(key_name)
            else f"API key: not set ({key_name or '-'})"
        ),
    ]
    if base_url:
        label, value = base_url
        lines.append(f"{label}: {value}")
    lines.extend([
        f"Proxy: {proxy_url()}",
        "",
        f"Model: {runtime_model()}",
        model_compatibility_line(provider),
        "IDE: Not connected",
        "MCP servers: none",
        f"Permission mode: {get_permission_mode()}",
        f"Setting sources: {setting_sources()}",
    ])
    return lines


def model_compatibility_line(provider: str) -> str:
    try:
        validate_model(provider, runtime_model())
    except ValueError as error:
        return f"Model compatibility: incompatible ({error})"
    return "Model compatibility: compatible"


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
    mark_setting_source("MODEL_ID", "User env")
    ui.print_info(f"model: {model}")


def handle_provider_command(args: list[str], context: RunContext | None = None):
    if not args:
        ui.print_info(f"provider: {configured_provider()}")
        return

    provider = args[0].lower()
    key_name = provider_key_name(provider)
    if key_name is None:
        ui.print_error(f"Unsupported provider: {provider}; choose anthropic, openai, or gemini")
        return
    if not os.getenv(key_name):
        ui.print_error(f"{key_name} is not configured. Set it with /api-key {provider} KEY first.")
        return

    model = " ".join(args[1:]).strip() or runtime_model()
    try:
        validate_model(provider, model)
    except ValueError:
        ui.print_error(f"Model {model!r} is not compatible with {provider}. Use: /provider {provider} MODEL_ID")
        return

    old_provider = os.environ.get("LLM_PROVIDER")
    old_model = os.environ.get("MODEL_ID")
    os.environ["LLM_PROVIDER"] = provider
    os.environ["MODEL_ID"] = model
    try:
        set_runtime_provider(provider, model)
    except Exception as error:
        if old_provider is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = old_provider
        if old_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = old_model
        ui.print_error(str(error))
        return

    set_env_value("LLM_PROVIDER", provider)
    set_env_value("MODEL_ID", model)
    mark_setting_source("LLM_PROVIDER", "User env")
    mark_setting_source("MODEL_ID", "User env")
    ui.print_info(f"provider: {provider}")


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def handle_api_key_command(args: list[str], context: RunContext | None = None):
    provider = configured_provider()
    if args and provider_key_name(args[0].lower()) is not None:
        provider = args[0].lower()
        args = args[1:]
    key_name = provider_key_name(provider)
    assert key_name is not None
    if not args:
        value = os.getenv(key_name, "")
        if value:
            ui.print_info(f"api-key: {mask_secret(value)}")
        else:
            ui.print_info("api-key: not set")
        return

    api_key = " ".join(args).strip()
    if not api_key:
        ui.print_error(f"Usage: /api-key [{provider}] KEY")
        return

    os.environ[key_name] = api_key
    set_env_value(key_name, api_key)
    mark_setting_source(key_name, "User env")
    if provider == configured_provider():
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
    "/provider": LocalCommand(
        name="/provider",
        description="Show or switch provider",
        handler=handle_provider_command,
    ),
    "/api-key": LocalCommand(
        name="/api-key",
        description="Show or set a Provider API key",
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
