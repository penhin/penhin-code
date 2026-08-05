import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.cli.commands import _handlers as commands
from penhin.cli.commands import router
from penhin.auth import ApiKeyCredential, InMemoryCredentialStore
from penhin.runtime.retry import CircuitBreaker
from penhin.agent.context import RunContext
from prompt_toolkit.document import Document
from penhin.tools.execution import ApprovalFlow, PermissionPolicy
from penhin.agent.session_manager import SessionManager
from penhin.agent.session_store import SessionStore


def empty_context() -> RunContext:
    return RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )


def test_handle_local_command_ignores_normal_input() -> None:
    assert router.handle_local_command("hello") is False


def test_handle_local_command_shows_help() -> None:
    with patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info:
        assert router.handle_local_command("/help") is True

    mocked_print_info.assert_any_call("/workspace Show workspace summary")
    mocked_print_info.assert_any_call("/permission Show or set permission mode")
    mocked_print_info.assert_any_call("/help Show local commands")
    mocked_print_info.assert_any_call("/status Show session and runtime status")
    mocked_print_info.assert_any_call("/model Select a model")
    mocked_print_info.assert_any_call("/provider Show or switch provider")
    mocked_print_info.assert_any_call("/circuit Show circuit breaker status")
    mocked_print_info.assert_any_call("/compact Compact current session, optionally with a hint")
    mocked_print_info.assert_any_call("/force-snip Mark selected history turns as snipped")
    mocked_print_info.assert_any_call("/tree Show the session tree or branch from an entry")
    mocked_print_info.assert_any_call("/fork Fork the session from an entry")


def test_handle_local_command_shows_workspace() -> None:
    workspace = {"cwd": "/tmp/project"}

    with patch("penhin.cli.commands._handlers.workspace_info", return_value=workspace), patch("penhin.cli.commands._handlers.ui.print_json") as mocked_print_json:
        assert router.handle_local_command("/workspace") is True

    mocked_print_json.assert_called_once_with(workspace)


def test_handle_local_command_shows_status() -> None:
    context = empty_context()
    context.session_path = Path(".penhin/sessions/session_demo-session.jsonl")

    class Runtime:
        model = "claude-test"

    with (
        patch.dict(
            "penhin.cli.commands._handlers.os.environ",
            {
                "PENHIN_VERSION": "2.6.11",
                "LLM_PROVIDER": "anthropic",
                "ANTHROPIC_BASE_URL": "https://api.example.com/v1",
                "HTTPS_PROXY": "http://127.0.0.1:15715",
            },
            clear=True,
        ),
        patch("penhin.cli.commands._handlers.workspace_info", return_value={"cwd": "/tmp/project"}),
        patch("penhin.cli.commands._handlers.runtime_manager.current", return_value=Runtime()),
        patch("penhin.cli.commands._handlers.get_permission_mode", return_value="default"),
        patch("penhin.cli.commands._handlers.auth_resolver", return_value=SimpleNamespace(status=lambda _provider: {"configured": False, "type": None, "source": "not set", "backend": None})),
        patch("penhin.cli.commands._handlers.setting_sources", return_value="User config, Project env"),
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/status", context) is True

    lines = [call.args[0] for call in mocked_print_info.call_args_list]
    assert lines == [
        "Version: 2.6.11",
        "Session name: /rename to add a name",
        "Session ID: demo-session",
        "cwd: /tmp/project",
        "API provider: Anthropic API",
        "Authentication: not configured (-; not set)",
        "Anthropic base URL: https://api.example.com/v1",
        "Proxy: http://127.0.0.1:15715",
        "",
        "Model: claude-test",
        "Thinking: -",
        "Model compatibility: compatible",
        "IDE: Not connected",
        "MCP servers: none",
        "Permission mode: default",
        "Setting sources: User config, Project env",
    ]


def test_status_uses_openai_compatible_base_url() -> None:
    with patch.dict(
        "penhin.cli.commands._handlers.os.environ",
        {
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
        },
        clear=True,
    ), patch("penhin.cli.commands._handlers.auth_resolver", return_value=SimpleNamespace(status=lambda _provider: {"configured": False, "type": None, "source": "not set", "backend": None})):
        assert commands.build_status_lines()[4:7] == [
            "API provider: OpenAI API",
        "Authentication: not configured (-; not set)",
            "OpenAI base URL: https://api.deepseek.com/v1",
        ]


def test_handle_local_command_reports_unknown_command() -> None:
    with patch("penhin.cli.commands._handlers.ui.print_error") as mocked_print_error:
        assert router.handle_local_command("/missing") is True

    mocked_print_error.assert_called_once_with("Unknown command: /missing")


def test_removed_auth_compatibility_commands_are_unknown() -> None:
    with patch("penhin.cli.commands._handlers.ui.print_error") as mocked_print_error:
        assert router.handle_local_command("/api-key") is True
        assert router.handle_local_command("/auth migrate") is True

    assert [call.args[0] for call in mocked_print_error.call_args_list] == [
        "Unknown command: /api-key",
        "Unknown auth command: migrate",
    ]


def test_handle_permission_command_shows_current_mode() -> None:
    with (
        patch("penhin.cli.commands._handlers.get_permission_mode", return_value="auto-review"),
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/permission") is True

    mocked_print_info.assert_called_once_with("permission: auto-review")


def test_handle_permission_command_updates_config_and_context() -> None:
    context = empty_context()

    with (
        patch("penhin.cli.commands._handlers.set_permission_mode") as mocked_set_permission_mode,
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/permission full-access", context) is True

    mocked_set_permission_mode.assert_called_once_with("full-access")
    mocked_print_info.assert_called_once_with("permission: full-access")
    assert "write" in context.policy.allow
    assert context.approval.is_approved("write", {"path": "demo.txt", "content": "hello"})


def test_handle_permission_command_rejects_unknown_mode() -> None:
    with (
        patch("penhin.cli.commands._handlers.ui.print_error") as mocked_print_error,
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/permission unsafe") is True

    mocked_print_error.assert_called_once_with("Unknown permission mode: unsafe")
    mocked_print_info.assert_called_once()
    assert "verification" not in mocked_print_info.call_args.args[0]


def test_handle_circuit_command_shows_disabled_state() -> None:
    class RuntimeWithoutBreaker:
        circuit_breaker = None
        compact_circuit_breaker = None

    with (
        patch("penhin.cli.commands._handlers.runtime_manager.current", return_value=RuntimeWithoutBreaker()),
        patch("penhin.cli.commands._handlers.ui.print_json") as mocked_print_json,
    ):
        assert router.handle_local_command("/circuit") is True

    mocked_print_json.assert_called_once_with({
        "main": {"enabled": False},
        "compact": {"enabled": False},
    })


def test_handle_circuit_command_shows_breaker_snapshot() -> None:
    class RuntimeWithBreaker:
        circuit_breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        compact_circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=20)

    RuntimeWithBreaker.circuit_breaker.record_failure()
    RuntimeWithBreaker.compact_circuit_breaker.record_failure()

    with (
        patch("penhin.cli.commands._handlers.runtime_manager.current", return_value=RuntimeWithBreaker()),
        patch("penhin.cli.commands._handlers.ui.print_json") as mocked_print_json,
    ):
        assert router.handle_local_command("/circuit") is True

    status = mocked_print_json.call_args.args[0]
    assert status["main"]["enabled"] is True
    assert status["main"]["state"] == "closed"
    assert status["main"]["failure_count"] == 1
    assert status["compact"]["enabled"] is True
    assert status["compact"]["failure_count"] == 1


def test_handle_compact_command_requires_context() -> None:
    with patch("penhin.cli.commands._handlers.ui.print_error") as mocked_print_error:
        assert router.handle_local_command("/compact keep decisions") is True

    mocked_print_error.assert_called_once_with("No active session to compact.")


def test_handle_compact_command_uses_hint() -> None:
    context = empty_context()

    with (
        patch.object(context, "force_auto_compact") as mocked_force_compact,
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/compact keep API cache details", context) is True

    mocked_force_compact.assert_called_once_with(hint="keep API cache details")
    mocked_print_info.assert_called_once_with("compact: done with hint")


def test_handle_model_command_updates_config_and_runtime() -> None:
    with (
        patch("penhin.cli.commands._handlers.runtime_manager.set_model") as mocked_set_runtime_model,
        patch("penhin.cli.commands._handlers.set_provider_model") as mocked_set_provider_model,
        patch("penhin.cli.commands._handlers.runtime_manager.configured_provider", return_value="anthropic"),
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/model claude-test") is True

    mocked_set_runtime_model.assert_called_once_with("claude-test")
    mocked_set_provider_model.assert_called_once_with("anthropic", "claude-test")
    mocked_print_info.assert_called_once_with("model: anthropic/claude-test")


def test_handle_model_command_switches_provider_with_pi_style_reference() -> None:
    with (
        patch("penhin.cli.commands._handlers.runtime_manager.configured_provider", return_value="anthropic"),
        patch("penhin.cli.commands._handlers._apply_provider_selection") as apply_selection,
        patch("penhin.cli.commands._handlers.ui.print_info") as print_info,
    ):
        assert router.handle_local_command("/model deepseek/deepseek-v4-pro:max") is True

    apply_selection.assert_called_once_with("deepseek", "deepseek-v4-pro", "max")
    print_info.assert_called_once_with("model: deepseek/deepseek-v4-pro · thinking max")


def test_login_refuses_to_collect_credentials_without_storage_consent() -> None:
    provider = Mock()
    with (
        patch("penhin.cli.commands._handlers.credential_store", side_effect=commands.CredentialStoreUnavailable("unavailable")),
        patch("penhin.cli.commands._handlers.ui.prompt_confirm", return_value=False),
        patch("penhin.cli.commands._handlers.provider_auth", return_value=provider),
        patch("penhin.cli.commands._handlers.ui.print_info") as print_info,
        patch("penhin.cli.commands._handlers.ui.print_error") as print_error,
    ):
        commands._login_api_key("openai")

    provider.login.assert_not_called()
    assert "not encrypted" in print_info.call_args.args[0]
    assert print_error.call_args.args[0] == "login cancelled: no credential storage backend was selected"


def test_login_storage_consent_selects_file_before_collecting_secret() -> None:
    store = InMemoryCredentialStore()
    provider = Mock()
    provider.login.return_value = ApiKeyCredential(key="stored-secret")
    with (
        patch("penhin.cli.commands._handlers.credential_store", side_effect=commands.CredentialStoreUnavailable("unavailable")),
        patch("penhin.cli.commands._handlers.ui.prompt_confirm", return_value=True),
        patch("penhin.cli.commands._handlers.set_credential_backend") as set_backend,
        patch("penhin.cli.commands._handlers.FileCredentialStore", return_value=store),
        patch("penhin.cli.commands._handlers.provider_auth", return_value=provider),
        patch("penhin.cli.commands._handlers._select_provider_model", return_value="gpt-5.4"),
        patch("penhin.cli.commands._handlers._apply_provider_selection"),
        patch("penhin.cli.commands._handlers.ui.print_info"),
    ):
        commands._login_api_key("openai")

    set_backend.assert_called_once_with("file")
    provider.login.assert_called_once()
    assert store.read("openai") == ApiKeyCredential(key="stored-secret")


def test_login_chooses_authentication_type_before_api_key_provider() -> None:
    with (
        patch("penhin.cli.commands._handlers.ui.prompt_select", side_effect=["api_key", "gemini"]) as prompt_select,
        patch("penhin.cli.commands._handlers._login_api_key") as login_api_key,
    ):
        commands.handle_login_command([])

    assert prompt_select.call_args_list[0].args == (
        "Choose authentication type",
        (("oauth", "Account"), ("api_key", "API key")),
    )
    assert prompt_select.call_args_list[1].args == (
        "Choose a provider",
        (
            ("anthropic", "Anthropic API key"),
            ("openai", "OpenAI API key"),
            ("gemini", "Google Gemini API key"),
            ("deepseek", "DeepSeek API key"),
        ),
    )
    login_api_key.assert_called_once_with("gemini")


def test_login_account_only_lists_account_providers() -> None:
    with (
        patch("penhin.cli.commands._handlers.ui.prompt_select", side_effect=["oauth", "anthropic"]) as prompt_select,
        patch("penhin.cli.commands._handlers._login_oauth") as login_oauth,
    ):
        commands.handle_login_command([])

    assert prompt_select.call_args_list[1].args == (
        "Choose a provider",
        (
            ("anthropic", "Claude Pro/Max account"),
            ("openai-codex", "ChatGPT Plus/Pro account"),
        ),
    )
    login_oauth.assert_called_once_with("anthropic")


def test_login_anthropic_still_asks_for_authentication_type() -> None:
    with (
        patch("penhin.cli.commands._handlers.ui.prompt_select", return_value="oauth") as prompt_select,
        patch("penhin.cli.commands._handlers._login_oauth") as login_oauth,
    ):
        commands.handle_login_command(["anthropic"])

    prompt_select.assert_called_once_with(
        "Choose authentication type",
        (("oauth", "Account"), ("api_key", "API key")),
    )
    login_oauth.assert_called_once_with("anthropic")


def test_login_single_method_provider_skips_authentication_type_prompt() -> None:
    with (
        patch("penhin.cli.commands._handlers.ui.prompt_select") as prompt_select,
        patch("penhin.cli.commands._handlers._login_api_key") as login_api_key,
    ):
        commands.handle_login_command(["openai"])

    prompt_select.assert_not_called()
    login_api_key.assert_called_once_with("openai")


def test_account_login_continues_to_selected_protocol_method() -> None:
    credential = SimpleNamespace(type="oauth")
    oauth = Mock()
    oauth.login.return_value = credential
    with (
        patch("penhin.cli.commands._handlers.ui.prompt_select", return_value="device_code") as prompt_select,
        patch("penhin.cli.commands._handlers._writable_store", return_value=InMemoryCredentialStore()),
        patch("penhin.cli.commands._handlers.provider_auth", return_value=oauth),
        patch("penhin.cli.commands._handlers._save_login") as save_login,
    ):
        commands._login_oauth("openai-codex")

    prompt_select.assert_called_once_with(
        "Choose account login method",
        (("browser", "Browser login"), ("device_code", "Device code login (headless)")),
    )
    assert oauth.login.call_args.args[0] == "oauth"
    assert oauth.login.call_args.kwargs == {"oauth_method": "device_code"}
    assert save_login.call_args.args[:2] == ("openai-codex", credential)


def test_terminal_auth_always_shows_url_and_only_attempts_browser_launch() -> None:
    interaction = commands.TerminalAuthInteraction()
    with (
        patch("penhin.cli.commands._handlers.ui.print_auth_url") as print_auth_url,
        patch("penhin.cli.commands._handlers.open_browser") as open_browser,
        patch("penhin.cli.commands._handlers.ui.print_info") as print_info,
    ):
        interaction.notify(
            "auth_url",
            url="https://provider.example/authorize?state=temporary",
            instructions="Complete login in your browser.",
        )

    print_auth_url.assert_called_once_with(
        "https://provider.example/authorize?state=temporary",
        "Complete login in your browser.",
    )
    open_browser.assert_called_once_with("https://provider.example/authorize?state=temporary")
    print_info.assert_not_called()


def test_handle_provider_command_switches_provider_and_model() -> None:
    with (
        patch.dict("penhin.cli.commands._handlers.os.environ", {"LLM_PROVIDER": "anthropic", "MODEL_ID": "claude-test", "OPENAI_API_KEY": "sk-openai"}, clear=True),
        patch("penhin.cli.commands._handlers.runtime_manager.switch_provider") as mocked_set_runtime_provider,
        patch("penhin.cli.commands._handlers.set_active_provider") as mocked_set_active_provider,
        patch("penhin.cli.commands._handlers.set_provider_model") as mocked_set_provider_model,
        patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info,
    ):
        assert router.handle_local_command("/provider openai gpt-4.1") is True

    mocked_set_runtime_provider.assert_called_once_with("openai", "gpt-4.1")
    mocked_set_active_provider.assert_called_once_with("openai")
    mocked_set_provider_model.assert_called_once_with("openai", "gpt-4.1")
    mocked_print_info.assert_called_once_with("provider: openai")


def test_handle_provider_command_selects_model_when_provider_has_no_saved_model() -> None:
    with (
        patch("penhin.cli.commands._handlers.get_provider_model", return_value=""),
        patch("penhin.cli.commands._handlers._select_provider_model", return_value="gpt-5.4") as select_model,
        patch("penhin.cli.commands._handlers._apply_provider_selection") as apply_selection,
        patch("penhin.cli.commands._handlers.ui.print_info"),
    ):
        assert router.handle_local_command("/provider openai") is True

    select_model.assert_called_once_with("openai")
    apply_selection.assert_called_once_with("openai", "gpt-5.4")


def test_model_command_without_argument_opens_provider_model_selector() -> None:
    with (
        patch("penhin.cli.commands._handlers.runtime_manager.configured_provider", return_value="anthropic"),
        patch("penhin.cli.commands._handlers._select_model_provider", return_value="anthropic"),
        patch("penhin.cli.commands._handlers._select_provider_model", return_value="claude-sonnet-5") as select_model,
        patch("penhin.cli.commands._handlers.runtime_manager.set_model"),
        patch("penhin.cli.commands._handlers.set_provider_model"),
        patch("penhin.cli.commands._handlers.ui.print_info"),
    ):
        assert router.handle_local_command("/model") is True

    select_model.assert_called_once_with("anthropic")


def test_handle_force_snip_lists_turns() -> None:
    context = empty_context()
    context.messages[:] = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second request"},
    ]

    with patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info:
        assert router.handle_local_command("/force-snip", context) is True

    mocked_print_info.assert_any_call("1: messages 1-2 first request")
    mocked_print_info.assert_any_call("2: messages 3-3 second request")


def test_handle_force_snip_marks_selected_turns() -> None:
    context = empty_context()
    context.messages[:] = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second request"},
    ]

    with patch("penhin.cli.commands._handlers.ui.print_info") as mocked_print_info:
        assert router.handle_local_command("/force-snip 1", context) is True

    assert context.messages[0]["_meta"]["snipped"] is True
    assert context.messages[1]["_meta"]["snipped"] is True
    assert "_meta" not in context.messages[2]
    mocked_print_info.assert_called_once_with("snip: marked 2 messages")


def test_tree_command_moves_leaf_and_next_append_creates_branch(tmp_path: Path) -> None:
    manager = SessionManager.create(tmp_path)
    root = manager.append_message({"role": "user", "content": "question"})
    original = manager.append_message({"role": "assistant", "content": "original"})
    context = empty_context()
    context.session_manager = manager
    context.session_path = manager.path
    context.messages = manager.build_context()

    with patch("penhin.cli.commands._handlers.ui.print_info"):
        assert router.handle_local_command(f"/tree {root[:6]}", context) is True

    assert manager.leaf_id == root
    assert context.messages == [{"role": "user", "content": "question"}]
    alternate = manager.append_message({"role": "assistant", "content": "alternate"})
    assert {entry["id"] for entry in manager.children(root)} == {original, alternate}


def test_fork_and_rename_commands_replace_active_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = store.new([{"role": "user", "content": "question"}])
    context = empty_context()
    context.session_manager = manager
    context.session_path = manager.path
    context.messages = manager.build_context()

    with (
        patch("penhin.cli.commands._handlers.sessions", store),
        patch("penhin.cli.commands._handlers.ui.print_info"),
    ):
        assert router.handle_local_command("/fork", context) is True
        assert router.handle_local_command("/rename alternate approach", context) is True

    assert context.session_manager is not manager
    assert context.session_manager.header["parentSession"] == str(manager.path)
    assert context.session_manager.get_session_name() == "alternate approach"
    assert context.messages == [{"role": "user", "content": "question"}]


def test_setup_command_completion_returns_completer() -> None:
    completer = router.setup_command_completion()
    completions = list(completer.get_completions(Document("/wo"), None))
    assert [completion.text for completion in completions] == ["/workspace"]
