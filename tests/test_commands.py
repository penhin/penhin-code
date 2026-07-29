import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands
from circuit_breaker import CircuitBreaker
from context import RunContext
from prompt_toolkit.document import Document
from tool_runtime import ApprovalFlow, PermissionPolicy


def empty_context() -> RunContext:
    return RunContext(
        messages=[],
        policy=PermissionPolicy(allow=set(), deny=set()),
        approval=ApprovalFlow.require_confirmation(set()),
    )


def test_handle_local_command_ignores_normal_input() -> None:
    assert commands.handle_local_command("hello") is False


def test_handle_local_command_shows_help() -> None:
    with patch("commands.ui.print_info") as mocked_print_info:
        assert commands.handle_local_command("/help") is True

    mocked_print_info.assert_any_call("/workspace Show workspace summary")
    mocked_print_info.assert_any_call("/permission Show or set permission mode")
    mocked_print_info.assert_any_call("/help Show local commands")
    mocked_print_info.assert_any_call("/status Show session and runtime status")
    mocked_print_info.assert_any_call("/model Show or set model id")
    mocked_print_info.assert_any_call("/provider Show or switch provider")
    mocked_print_info.assert_any_call("/api-key Show or set a Provider API key")
    mocked_print_info.assert_any_call("/circuit Show circuit breaker status")
    mocked_print_info.assert_any_call("/compact Compact current session, optionally with a hint")
    mocked_print_info.assert_any_call("/force-snip Mark selected history turns as snipped")


def test_handle_local_command_shows_workspace() -> None:
    workspace = {"cwd": "/tmp/project"}

    with patch("commands.workspace_info", return_value=workspace), patch("commands.ui.print_json") as mocked_print_json:
        assert commands.handle_local_command("/workspace") is True

    mocked_print_json.assert_called_once_with(workspace)


def test_handle_local_command_shows_status() -> None:
    context = empty_context()
    context.session_path = Path(".transcripts/transcript_demo-session.jsonl")

    class Runtime:
        model = "claude-test"

    with (
        patch.dict(
            "commands.os.environ",
            {
                "PENHIN_VERSION": "2.6.11",
                "LLM_PROVIDER": "anthropic",
                "ANTHROPIC_BASE_URL": "https://api.example.com/v1",
                "HTTPS_PROXY": "http://127.0.0.1:15715",
            },
            clear=True,
        ),
        patch("commands.workspace_info", return_value={"cwd": "/tmp/project"}),
        patch("commands.get_runtime", return_value=Runtime()),
        patch("commands.get_permission_mode", return_value="default"),
        patch("commands.setting_sources", return_value="User config, Project env"),
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/status", context) is True

    lines = [call.args[0] for call in mocked_print_info.call_args_list]
    assert lines == [
        "Version: 2.6.11",
        "Session name: /rename to add a name",
        "Session ID: demo-session",
        "cwd: /tmp/project",
        "API provider: Anthropic API",
        "API key: not set (ANTHROPIC_API_KEY)",
        "Anthropic base URL: https://api.example.com/v1",
        "Proxy: http://127.0.0.1:15715",
        "",
        "Model: claude-test",
        "Model compatibility: compatible",
        "IDE: Not connected",
        "MCP servers: none",
        "Permission mode: default",
        "Setting sources: User config, Project env",
    ]


def test_status_uses_openai_compatible_base_url() -> None:
    with patch.dict(
        "commands.os.environ",
        {
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
        },
        clear=True,
    ):
        assert commands.build_status_lines()[4:7] == [
            "API provider: OpenAI API",
            "API key: not set (OPENAI_API_KEY)",
            "OpenAI base URL: https://api.deepseek.com/v1",
        ]


def test_handle_local_command_reports_unknown_command() -> None:
    with patch("commands.ui.print_error") as mocked_print_error:
        assert commands.handle_local_command("/missing") is True

    mocked_print_error.assert_called_once_with("Unknown command: /missing")


def test_handle_permission_command_shows_current_mode() -> None:
    with (
        patch("commands.get_permission_mode", return_value="auto-review"),
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/permission") is True

    mocked_print_info.assert_called_once_with("permission: auto-review")


def test_handle_permission_command_updates_config_and_context() -> None:
    context = empty_context()

    with (
        patch("commands.set_permission_mode") as mocked_set_permission_mode,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/permission full-access", context) is True

    mocked_set_permission_mode.assert_called_once_with("full-access")
    mocked_print_info.assert_called_once_with("permission: full-access")
    assert "write" in context.policy.allow
    assert context.approval.is_approved("write", {"path": "demo.txt", "content": "hello"})


def test_handle_permission_command_rejects_unknown_mode() -> None:
    with (
        patch("commands.ui.print_error") as mocked_print_error,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/permission unsafe") is True

    mocked_print_error.assert_called_once_with("Unknown permission mode: unsafe")
    mocked_print_info.assert_called_once()
    assert "verification" not in mocked_print_info.call_args.args[0]


def test_handle_circuit_command_shows_disabled_state() -> None:
    class RuntimeWithoutBreaker:
        circuit_breaker = None
        compact_circuit_breaker = None

    with (
        patch("commands.get_runtime", return_value=RuntimeWithoutBreaker()),
        patch("commands.ui.print_json") as mocked_print_json,
    ):
        assert commands.handle_local_command("/circuit") is True

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
        patch("commands.get_runtime", return_value=RuntimeWithBreaker()),
        patch("commands.ui.print_json") as mocked_print_json,
    ):
        assert commands.handle_local_command("/circuit") is True

    status = mocked_print_json.call_args.args[0]
    assert status["main"]["enabled"] is True
    assert status["main"]["state"] == "closed"
    assert status["main"]["failure_count"] == 1
    assert status["compact"]["enabled"] is True
    assert status["compact"]["failure_count"] == 1


def test_handle_compact_command_requires_context() -> None:
    with patch("commands.ui.print_error") as mocked_print_error:
        assert commands.handle_local_command("/compact keep decisions") is True

    mocked_print_error.assert_called_once_with("No active session to compact.")


def test_handle_compact_command_uses_hint() -> None:
    context = empty_context()

    with (
        patch.object(context, "force_auto_compact") as mocked_force_compact,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/compact keep API cache details", context) is True

    mocked_force_compact.assert_called_once_with(hint="keep API cache details")
    mocked_print_info.assert_called_once_with("compact: done with hint")


def test_handle_model_command_updates_config_and_runtime() -> None:
    with (
        patch("commands.set_env_value") as mocked_set_env_value,
        patch("commands.set_runtime_model") as mocked_set_runtime_model,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/model claude-test") is True

    mocked_set_env_value.assert_called_once_with("MODEL_ID", "claude-test")
    mocked_set_runtime_model.assert_called_once_with("claude-test")
    mocked_print_info.assert_called_once_with("model: claude-test")


def test_handle_api_key_command_saves_without_echoing_secret() -> None:
    with (
        patch("commands.set_env_value") as mocked_set_env_value,
        patch("commands.set_runtime_api_key") as mocked_set_runtime_api_key,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/api-key sk-ant-secret") is True

    mocked_set_env_value.assert_called_once_with("ANTHROPIC_API_KEY", "sk-ant-secret")
    mocked_set_runtime_api_key.assert_called_once_with("sk-ant-secret")
    mocked_print_info.assert_called_once_with("api-key: saved")


def test_handle_api_key_command_saves_a_non_active_provider_key() -> None:
    with (
        patch.dict("commands.os.environ", {"LLM_PROVIDER": "anthropic"}, clear=True),
        patch("commands.set_env_value") as mocked_set_env_value,
        patch("commands.set_runtime_api_key") as mocked_set_runtime_api_key,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/api-key openai sk-openai-secret") is True

    mocked_set_env_value.assert_called_once_with("OPENAI_API_KEY", "sk-openai-secret")
    mocked_set_runtime_api_key.assert_not_called()
    mocked_print_info.assert_called_once_with("api-key: saved")


def test_handle_provider_command_switches_provider_and_model() -> None:
    with (
        patch.dict("commands.os.environ", {"LLM_PROVIDER": "anthropic", "MODEL_ID": "claude-test", "OPENAI_API_KEY": "sk-openai"}, clear=True),
        patch("commands.set_runtime_provider") as mocked_set_runtime_provider,
        patch("commands.set_env_value") as mocked_set_env_value,
        patch("commands.ui.print_info") as mocked_print_info,
    ):
        assert commands.handle_local_command("/provider openai gpt-4.1") is True

    mocked_set_runtime_provider.assert_called_once_with("openai", "gpt-4.1")
    assert mocked_set_env_value.call_args_list[0].args == ("LLM_PROVIDER", "openai")
    assert mocked_set_env_value.call_args_list[1].args == ("MODEL_ID", "gpt-4.1")
    mocked_print_info.assert_called_once_with("provider: openai")


def test_handle_provider_command_requires_a_matching_model_and_key() -> None:
    with patch.dict("commands.os.environ", {"MODEL_ID": "claude-test", "OPENAI_API_KEY": "sk-openai"}, clear=True), patch("commands.ui.print_error") as mocked_print_error:
        assert commands.handle_local_command("/provider openai") is True

    mocked_print_error.assert_called_once_with("Model 'claude-test' is not compatible with openai. Use: /provider openai MODEL_ID")


def test_handle_force_snip_lists_turns() -> None:
    context = empty_context()
    context.messages[:] = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second request"},
    ]

    with patch("commands.ui.print_info") as mocked_print_info:
        assert commands.handle_local_command("/force-snip", context) is True

    mocked_print_info.assert_any_call("1: messages 1-2 first request")
    mocked_print_info.assert_any_call("2: messages 3-3 second request")


def test_handle_force_snip_marks_selected_turns() -> None:
    context = empty_context()
    context.messages[:] = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second request"},
    ]

    with patch("commands.ui.print_info") as mocked_print_info:
        assert commands.handle_local_command("/force-snip 1", context) is True

    assert context.messages[0]["_meta"]["snipped"] is True
    assert context.messages[1]["_meta"]["snipped"] is True
    assert "_meta" not in context.messages[2]
    mocked_print_info.assert_called_once_with("snip: marked 2 messages")


def test_complete_local_command_matches_prefix() -> None:
    assert commands.complete_local_command("/w", 0) == "/workspace"
    assert commands.complete_local_command("/wo", 0) == "/workspace"
    assert commands.complete_local_command("/h", 0) == "/help"
    assert commands.complete_local_command("/st", 0) == "/status"
    assert commands.complete_local_command("/mo", 0) == "/model"
    assert commands.complete_local_command("/pr", 0) == "/provider"
    assert commands.complete_local_command("/api", 0) == "/api-key"
    assert commands.complete_local_command("/co", 0) == "/compact"
    assert commands.complete_local_command("/force", 0) == "/force-snip"
    assert commands.complete_local_command("/x", 0) is None
    assert commands.complete_local_command("/workspace", 1) is None


def test_setup_command_completion_registers_completer() -> None:
    with patch("commands.ui.set_prompt_completer") as mocked_set_prompt_completer:
        completer = commands.setup_command_completion()

    mocked_set_prompt_completer.assert_called_once_with(completer)

    completions = list(completer.get_completions(Document("/wo"), None))
    assert [completion.text for completion in completions] == ["/workspace"]


def run_all() -> None:
    test_handle_local_command_ignores_normal_input()
    test_handle_local_command_shows_help()
    test_handle_local_command_shows_workspace()
    test_handle_local_command_shows_status()
    test_status_uses_openai_compatible_base_url()
    test_handle_local_command_reports_unknown_command()
    test_handle_permission_command_shows_current_mode()
    test_handle_permission_command_updates_config_and_context()
    test_handle_permission_command_rejects_unknown_mode()
    test_handle_circuit_command_shows_disabled_state()
    test_handle_circuit_command_shows_breaker_snapshot()
    test_handle_compact_command_requires_context()
    test_handle_compact_command_uses_hint()
    test_handle_model_command_updates_config_and_runtime()
    test_handle_api_key_command_saves_without_echoing_secret()
    test_handle_api_key_command_saves_a_non_active_provider_key()
    test_handle_provider_command_switches_provider_and_model()
    test_handle_provider_command_requires_a_matching_model_and_key()
    test_handle_force_snip_lists_turns()
    test_handle_force_snip_marks_selected_turns()
    test_complete_local_command_matches_prefix()
    test_setup_command_completion_registers_completer()


if __name__ == "__main__":
    run_all()
    print("ok")
