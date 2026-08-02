import os
import time
from pathlib import Path

from penhin.cli import ui

from penhin.infrastructure.config import (
    CONFIG_FILE, ENV_FILE, get_permission_mode, get_provider_model, get_provider_thinking_level, get_version,
    set_active_provider, set_credential_backend, set_permission_mode, set_provider_model,
    set_provider_thinking_level,
)
from penhin.auth import auth_resolver, credential_store, provider_auth, provider_auth_ids, provider_key_name
from penhin.auth.browser import open_browser
from penhin.auth.interaction import AuthInteraction
from penhin.auth.storage import CredentialStoreUnavailable, FileCredentialStore
from penhin.agent.context import RunContext, conversation_turn_ranges, parse_snip_selectors
from penhin.permissions import PERMISSION_MODES, PermissionMode, transition_mode
from penhin.runtime import runtime_manager
from penhin.providers.models import (
    model_options, model_thinking_levels, parse_model_reference, supports_custom_model, validate_model,
)
from penhin.tools.execution import runtime_permission_setup
from penhin.tools.registry import tool_names
from penhin.tools.builtin.workspace import workspace_info
from penhin.agent.transcript import session_id_from_path


def handle_workspace_command(args: list[str], context: RunContext | None = None):
    ui.print_json(workspace_info(tool_names()))


def provider_label(provider: str) -> str:
    labels = {
        "anthropic": "Anthropic API",
        "openai": "OpenAI API",
        "openai-codex": "OpenAI ChatGPT Plus/Pro",
        "gemini": "Gemini API",
        "deepseek": "DeepSeek API",
    }
    return labels.get(provider, provider or "-")


def provider_base_url(provider: str) -> tuple[str, str] | None:
    env_names = {
        "anthropic": ("ANTHROPIC_BASE_URL", "Anthropic base URL"),
        "openai": ("OPENAI_BASE_URL", "OpenAI base URL"),
        "openai-codex": ("PENHIN_OPENAI_CODEX_BASE_URL", "OpenAI Codex base URL"),
        "deepseek": ("DEEPSEEK_BASE_URL", "DeepSeek base URL"),
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
        return runtime_manager.current().model
    except RuntimeError:
        return get_provider_model(runtime_manager.configured_provider()) or "-"


def _select_provider_model(provider: str) -> str:
    current = get_provider_model(provider)
    options = tuple(
        (
            item.id,
            f"{item.name}  ·  {item.id}" + ("  (current)" if item.id == current else ""),
        )
        for item in model_options(provider)
    )
    if supports_custom_model(provider):
        options += (("__custom__", "Custom model ID"),)
    if not options:
        raise ValueError(f"No model catalog is available for {provider}")
    selected = ui.prompt_select(f"Choose a model for {provider}", options)
    if selected == "__custom__":
        selected = ui.prompt_text(f"Custom model ID for {provider}")
    validate_model(provider, selected)
    return selected


def _select_model_provider() -> str:
    current = runtime_manager.configured_provider()
    configured: list[str] = []
    for provider in provider_auth_ids():
        try:
            if auth_resolver().status(provider).get("configured"):
                configured.append(provider)
        except CredentialStoreUnavailable:
            continue
    if current not in configured:
        configured.insert(0, current)
    else:
        configured = [current, *(provider for provider in configured if provider != current)]
    if len(configured) == 1:
        return configured[0]
    return ui.prompt_select(
        "Choose a provider",
        tuple((provider, provider_label(provider)) for provider in configured),
    )


def _effective_thinking_level(provider: str, model: str, requested: str | None = None) -> str | None:
    levels = model_thinking_levels(provider, model)
    if not levels:
        return None
    saved = get_provider_thinking_level(provider)
    level = requested or (saved if saved in levels else "high" if "high" in levels else levels[0])
    if level not in levels:
        raise ValueError(f"Thinking level {level!r} is not supported; choose {', '.join(levels)}")
    return level


def build_status_lines(context: RunContext | None = None) -> list[str]:
    provider = runtime_manager.configured_provider()
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
        f"Thinking: {get_provider_thinking_level(provider) or '-'}",
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
    current_provider = runtime_manager.configured_provider()
    try:
        if args:
            reference = " ".join(args).strip()
            try:
                provider, model, requested_level = parse_model_reference(reference, current_provider)
            except ValueError:
                # Custom gateways retain direct model-ID support for the active provider.
                if "/" in reference or ":" in reference:
                    raise
                validate_model(current_provider, reference)
                provider, model, requested_level = current_provider, reference, None
        else:
            provider = _select_model_provider()
            model = _select_provider_model(provider)
            requested_level = None
        thinking_level = _effective_thinking_level(provider, model, requested_level)
    except (ValueError, KeyboardInterrupt) as error:
        ui.print_error("model selection cancelled" if isinstance(error, KeyboardInterrupt) else str(error))
        return

    try:
        if provider == current_provider:
            runtime_manager.set_model(model)
            runtime_manager.set_thinking_level(thinking_level)
            set_provider_model(provider, model)
            if thinking_level is not None:
                set_provider_thinking_level(provider, thinking_level)
        else:
            _apply_provider_selection(provider, model, thinking_level)
    except (ValueError, RuntimeError) as error:
        ui.print_error(str(error))
        return
    suffix = f" · thinking {thinking_level}" if thinking_level else ""
    ui.print_info(f"model: {provider}/{model}{suffix}")


def handle_thinking_command(args: list[str], context: RunContext | None = None):
    provider = runtime_manager.configured_provider()
    model = runtime_model()
    levels = model_thinking_levels(provider, model)
    if not levels:
        ui.print_error(f"{provider}/{model} does not expose configurable thinking levels")
        return
    try:
        level = args[0].lower() if args else ui.prompt_select(
            "Choose a thinking level",
            tuple((item, item + ("  (current)" if item == get_provider_thinking_level(provider) else "")) for item in levels),
        )
        if level not in levels:
            raise ValueError(f"Unsupported thinking level: {level}; choose {', '.join(levels)}")
    except (ValueError, KeyboardInterrupt) as error:
        ui.print_error("thinking selection cancelled" if isinstance(error, KeyboardInterrupt) else str(error))
        return
    runtime_manager.set_thinking_level(level)
    set_provider_thinking_level(provider, level)
    ui.print_info(f"thinking: {level}")


def handle_provider_command(args: list[str], context: RunContext | None = None):
    if not args:
        ui.print_info(f"provider: {runtime_manager.configured_provider()}")
        return

    provider = args[0].lower()
    if provider not in provider_auth_ids():
        ui.print_error(f"Unsupported provider: {provider}; choose anthropic, openai, openai-codex, gemini, or deepseek")
        return
    model = " ".join(args[1:]).strip()
    if not model:
        saved = get_provider_model(provider)
        try:
            if saved:
                validate_model(provider, saved)
                model = saved
            else:
                model = _select_provider_model(provider)
        except (ValueError, KeyboardInterrupt) as error:
            ui.print_error("model selection cancelled" if isinstance(error, KeyboardInterrupt) else str(error))
            return
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


def _apply_provider_selection(provider: str, model: str, thinking_level: str | None = None) -> None:
    runtime_manager.switch_provider(provider, model)
    level = _effective_thinking_level(provider, model, thinking_level)
    runtime_manager.set_thinking_level(level)
    set_active_provider(provider)
    set_provider_model(provider, model)
    if level is not None:
        set_provider_thinking_level(provider, level)


def _save_login(provider: str, credential, started: float | None = None, store=None) -> None:
    from penhin.evaluation.observer import emit
    store = store or _writable_store()
    model = _select_provider_model(provider)
    store.modify(provider, lambda _current: credential)
    _apply_provider_selection(provider, model)
    emit(
        "auth_login_completed", provider=provider, auth_type=credential.type,
        backend=store.backend_name, duration_ms=(time.perf_counter() - started) * 1000 if started is not None else None,
    )
    ui.print_info(f"login: {provider} authenticated via {credential.type}")


def _login_api_key(provider: str) -> None:
    from penhin.evaluation.observer import emit
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
        "deepseek": "DeepSeek API key",
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
    from penhin.evaluation.observer import emit

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
        from penhin.evaluation.observer import emit
        emit("auth_logout_completed", provider=provider, backend=store.backend_name)
        runtime_manager.initialize(required=False)
        ui.print_info(f"logout: removed local {provider} credential")
        key_name = provider_key_name(provider)
        if key_name and os.getenv(key_name):
            ui.print_info(f"warning: {key_name} is still configured in {runtime_manager.setting_source(key_name)}")
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
        runtime = runtime_manager.current()
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
