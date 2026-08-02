import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from prompt_toolkit.completion import Completer, Completion

import ui

from config import (
    CONFIG_FILE, ENV_FILE, get_permission_mode, get_provider_model, get_version,
    set_credential_backend, set_env_value, set_permission_mode, set_provider_model, update_env_values,
)
from auth import auth_resolver, credential_store, provider_auth, provider_auth_ids, provider_key_name
from auth.browser import open_browser
from auth.interaction import AuthInteraction
from auth.storage import CredentialStoreUnavailable, FileCredentialStore
from context import RunContext, conversation_turn_ranges, parse_snip_selectors
from permissions import PERMISSION_MODES, PermissionMode, transition_mode
from runtime import (
    configured_provider,
    get_runtime,
    mark_setting_source,
    set_runtime_model,
    set_runtime_provider,
    setting_source,
    init_runtime,
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
        "openai-codex": "OpenAI ChatGPT Plus/Pro",
        "gemini": "Gemini API",
    }
    return labels.get(provider, provider or "-")


def provider_base_url(provider: str) -> tuple[str, str] | None:
    env_names = {
        "anthropic": ("ANTHROPIC_BASE_URL", "Anthropic base URL"),
        "openai": ("OPENAI_BASE_URL", "OpenAI base URL"),
        "openai-codex": ("PENHIN_OPENAI_CODEX_BASE_URL", "OpenAI Codex base URL"),
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
    base_url = provider_base_url(provider)
    try:
        auth_status = auth_resolver().status(provider)
    except CredentialStoreUnavailable as error:
        auth_status = {"configured": False, "type": None, "source": str(error), "backend": None}
    lines = [
        f"Version: {get_version()}",
        "Session name: /rename to add a name",
        f"Session ID: {session_id(context)}",
        f"cwd: {workspace_info().get('cwd', '-')}",
        f"API provider: {provider_label(provider)}",
        f"Authentication: {'configured' if auth_status['configured'] else 'not configured'} "
        f"({auth_status.get('type') or '-'}; {auth_status.get('source') or 'not set'})",
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
    set_provider_model(configured_provider(), model)
    mark_setting_source("MODEL_ID", "User env")
    ui.print_info(f"model: {model}")


def handle_provider_command(args: list[str], context: RunContext | None = None):
    if not args:
        ui.print_info(f"provider: {configured_provider()}")
        return

    provider = args[0].lower()
    if provider not in provider_auth_ids():
        ui.print_error(f"Unsupported provider: {provider}; choose anthropic, openai, openai-codex, or gemini")
        return
    model = " ".join(args[1:]).strip() or runtime_model()
    try:
        validate_model(provider, model)
    except ValueError:
        ui.print_error(f"Model {model!r} is not compatible with {provider}. Use: /provider {provider} MODEL_ID")
        return

    try:
        _apply_provider_selection(provider, model)
    except Exception as error:
        ui.print_error(str(error))
        return
    ui.print_info(f"provider: {provider}")


class TerminalAuthInteraction(AuthInteraction):
    def prompt(self, kind: str, message: str, options: tuple[tuple[str, str], ...] = ()) -> str:
        if kind == "secret":
            return ui.prompt_secret(message)
        if kind == "select":
            return ui.prompt_select(message, options)
        return ui.prompt_text(message)

    async def prompt_async(self, kind: str, message: str, options: tuple[tuple[str, str], ...] = ()) -> str:
        if kind != "manual_code":
            raise ValueError(f"unsupported asynchronous auth prompt: {kind}")
        return await ui.prompt_text_async(message)

    def notify(self, kind: str, **payload: object) -> None:
        if kind == "auth_url":
            url = str(payload.get("url", ""))
            ui.print_auth_url(url, str(payload.get("instructions", "")))
            if url:
                open_browser(url)
        elif kind == "device_code":
            ui.print_device_code(str(payload.get("verification_uri", "")), str(payload.get("user_code", "")))
        elif kind == "progress":
            ui.print_info(str(payload.get("message", "")))


def _writable_store():
    try:
        return credential_store()
    except CredentialStoreUnavailable as error:
        ui.print_info(
            "The system keyring is unavailable. Penhin can use ~/.penhin/auth.json instead; "
            "the file is not encrypted, but its directory and file are restricted to your user."
        )
        if not ui.prompt_confirm("Use the protected file credential store (mode 0600)?"):
            raise CredentialStoreUnavailable("login cancelled: no credential storage backend was selected") from error
        set_credential_backend("file")
        return FileCredentialStore()


def _apply_provider_selection(provider: str, model: str) -> None:
    old_provider, old_model = os.environ.get("LLM_PROVIDER"), os.environ.get("MODEL_ID")
    os.environ["LLM_PROVIDER"], os.environ["MODEL_ID"] = provider, model
    try:
        set_runtime_provider(provider, model)
    except Exception:
        if old_provider is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = old_provider
        if old_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = old_model
        raise
    update_env_values({"LLM_PROVIDER": provider, "MODEL_ID": model})
    set_provider_model(provider, model)
    mark_setting_source("LLM_PROVIDER", "User env")
    mark_setting_source("MODEL_ID", "User env")


def _activate_login(provider: str) -> None:
    model = get_provider_model(provider)
    if not model:
        current = runtime_model()
        try:
            validate_model(provider, current)
            model = current
        except ValueError:
            model = ui.prompt_text(f"Model ID for {provider}")
    validate_model(provider, model)
    _apply_provider_selection(provider, model)


def _save_login(provider: str, credential, started: float | None = None, store=None) -> None:
    from evaluation.observer import emit
    store = store or _writable_store()
    store.modify(provider, lambda _current: credential)
    _activate_login(provider)
    emit(
        "auth_login_completed", provider=provider, auth_type=credential.type,
        backend=store.backend_name, duration_ms=(time.perf_counter() - started) * 1000 if started is not None else None,
    )
    ui.print_info(f"login: {provider} authenticated via {credential.type}")


def _login_api_key(provider: str) -> None:
    from evaluation.observer import emit
    started = time.perf_counter()
    emit("auth_login_started", provider=provider, auth_type="api_key")
    key_name = provider_key_name(provider)
    if key_name is None:
        ui.print_error(f"{provider} does not support API key login")
        return
    try:
        store = _writable_store()
        credential = provider_auth(provider).login("api_key", TerminalAuthInteraction())
        _save_login(provider, credential, started, store)
    except (Exception, KeyboardInterrupt) as error:
        emit("auth_login_failed", provider=provider, auth_type="api_key", error_type=type(error).__name__, duration_ms=(time.perf_counter() - started) * 1000)
        ui.print_error("login cancelled" if isinstance(error, KeyboardInterrupt) else str(error))


_LOGIN_PROVIDER_LABELS = {
    "api_key": {
        "anthropic": "Anthropic API key",
        "openai": "OpenAI API key",
        "gemini": "Google Gemini API key",
    },
    "oauth": {
        "anthropic": "Claude Pro/Max account",
        "openai-codex": "ChatGPT Plus/Pro account",
    },
}


def _login_provider_options(auth_type: str) -> tuple[tuple[str, str], ...]:
    labels = _LOGIN_PROVIDER_LABELS[auth_type]
    return tuple(
        (provider, labels.get(provider, provider_label(provider)))
        for provider in provider_auth_ids()
        if auth_type in provider_auth(provider).methods()
    )


def _choose_login_auth_type(provider: str = "") -> str:
    methods = provider_auth(provider).methods() if provider else ("api_key", "oauth")
    options = tuple(
        option for option in (
            ("oauth", "Account"),
            ("api_key", "API key"),
        )
        if option[0] in methods
    )
    if len(options) == 1:
        return options[0][0]
    return ui.prompt_select("Choose authentication type", options)


def _login_oauth(provider: str) -> None:
    from evaluation.observer import emit

    interaction = TerminalAuthInteraction()
    started = time.perf_counter()
    method = "browser"
    try:
        store = _writable_store()
        if provider == "openai-codex":
            method = ui.prompt_select(
                "Choose account login method",
                (("browser", "Browser login"), ("device_code", "Device code login (headless)")),
            )
        emit("auth_login_started", provider=provider, auth_type="oauth", method=method)
        credential = provider_auth(provider).login("oauth", interaction, oauth_method=method)
        _save_login(provider, credential, started, store)
    except (Exception, KeyboardInterrupt) as error:
        emit(
            "auth_login_failed",
            provider=provider,
            auth_type="oauth",
            error_type=type(error).__name__,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        ui.print_error("login cancelled" if isinstance(error, KeyboardInterrupt) else str(error))


def handle_login_command(args: list[str], context: RunContext | None = None):
    if len(args) > 1:
        ui.print_error("Usage: /login [provider]")
        return
    provider = args[0].lower() if args else ""
    if provider and provider not in provider_auth_ids():
        ui.print_error(f"Unsupported provider: {provider}")
        return
    try:
        auth_type = _choose_login_auth_type(provider)
        if not provider:
            provider = ui.prompt_select("Choose a provider", _login_provider_options(auth_type))
    except (ValueError, KeyboardInterrupt) as error:
        ui.print_error("login cancelled" if isinstance(error, KeyboardInterrupt) else str(error))
        return

    if auth_type == "api_key":
        _login_api_key(provider)
    else:
        _login_oauth(provider)


def handle_logout_command(args: list[str], context: RunContext | None = None):
    if len(args) > 1:
        ui.print_error("Usage: /logout [provider]")
        return
    try:
        store = credential_store()
        values = store.list()
        provider = args[0].lower() if args else ""
        if not provider:
            if not values:
                ui.print_info("logout: no stored credentials")
                return
            provider = ui.prompt_select("Choose a provider to log out", tuple((item, provider_label(item)) for item in values))
        if provider not in provider_auth_ids():
            ui.print_error(f"Unsupported provider: {provider}")
            return
        if provider not in values:
            ui.print_info(f"logout: no stored credential for {provider}")
            return
        store.delete(provider)
        from evaluation.observer import emit
        emit("auth_logout_completed", provider=provider, backend=store.backend_name)
        init_runtime(required=False)
        ui.print_info(f"logout: removed local {provider} credential")
        key_name = provider_key_name(provider)
        if key_name and os.getenv(key_name):
            ui.print_info(f"warning: {key_name} is still configured in {setting_source(key_name)}")
    except Exception as error:
        ui.print_error(str(error))


def _auth_status() -> None:
    for provider in provider_auth_ids():
        try:
            status = auth_resolver().status(provider)
        except CredentialStoreUnavailable as error:
            status = {"provider": provider, "configured": False, "type": None, "source": str(error), "backend": None, "expired": None}
        ui.print_json(status)


def handle_auth_command(args: list[str], context: RunContext | None = None):
    if not args or args == ["status"]:
        _auth_status()
    else:
        ui.print_error(f"Unknown auth command: {' '.join(args)}")


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


class LocalCommandCompleter(Completer):
    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return

        for name in LOCAL_COMMANDS:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text))


def setup_command_completion() -> LocalCommandCompleter:
    return LocalCommandCompleter()

    
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
    "/login": LocalCommand(
        name="/login",
        description="Configure provider authentication",
        handler=handle_login_command,
    ),
    "/logout": LocalCommand(
        name="/logout",
        description="Remove a stored provider credential",
        handler=handle_logout_command,
    ),
    "/auth": LocalCommand(
        name="/auth",
        description="Show authentication status",
        handler=handle_auth_command,
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
