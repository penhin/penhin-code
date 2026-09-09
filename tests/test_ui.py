import pytest
from types import SimpleNamespace
from unicodedata import east_asian_width

from penhin.cli import ui


def test_status_line_uses_the_active_runtime_context_window(monkeypatch) -> None:
    from penhin.runtime import runtime_manager

    monkeypatch.setattr(ui, "status_context", SimpleNamespace(messages=[]))
    monkeypatch.setattr(runtime_manager, "current", lambda: SimpleNamespace(context_window=200_000))
    monkeypatch.setattr(runtime_manager, "configured_provider", lambda: "openai")
    monkeypatch.setattr(runtime_manager, "status", lambda: SimpleNamespace(model="gpt-test"))
    monkeypatch.setattr("penhin.infrastructure.config.get_permission_mode", lambda: "default")

    assert "0.0% · 0.0k / 200k" in ui._status_line()


def test_status_line_allows_starting_before_runtime_authentication(monkeypatch) -> None:
    from penhin.runtime import AuthenticationRequired, runtime_manager

    monkeypatch.setattr(ui, "status_context", SimpleNamespace(messages=[]))
    monkeypatch.setattr(runtime_manager, "current", lambda: (_ for _ in ()).throw(AuthenticationRequired("login required")))
    monkeypatch.setattr(runtime_manager, "configured_provider", lambda: "not configured")
    monkeypatch.setattr(runtime_manager, "status", lambda: SimpleNamespace(model="not configured"))
    monkeypatch.setattr("penhin.infrastructure.config.get_permission_mode", lambda: "default")

    assert "0.0% · 0.0k / 128k" in ui._status_line()


def test_full_screen_transcript_keeps_structured_message_cards() -> None:
    terminal = ui.Transcript()

    terminal.add_message("user", "You", "Explain the change")
    stream = terminal.start_stream("DeepSeek")
    stream.write("I found the cause")
    stream.write(" in the terminal renderer.")
    stream.finish(tokens=42)
    terminal.add_message("system", "System", "Target changed")

    assert [(card.kind, card.name, card.content) for card in terminal.cards] == [
        ("user", "You", "Explain the change"),
        ("agent", "DeepSeek", "I found the cause in the terminal renderer."),
        ("system", "System", "Target changed"),
    ]
    assert terminal.cards[1].tokens == 42
    assert terminal.cards[1].color == "#3b82f6"


def test_full_screen_transcript_uses_provider_identity_colours() -> None:
    terminal = ui.Transcript()

    assert terminal.message_color("ChatGPT") == "#f8fafc"
    assert terminal.message_color("Claude Code") == "#f97316"
    assert terminal.message_color("Custom Agent") == terminal.message_color("Custom Agent")


def test_command_surface_filters_choices_without_mutating_transcript() -> None:
    surface = ui.SelectionSurface(
        "Choose a model",
        (("openai/gpt-5.6", "OpenAI - GPT-5.6"), ("deepseek/deepseek-v4", "DeepSeek - V4")),
        ui.Queue(maxsize=1),
        scroll_position=7,
    )

    assert surface.matching_options("deep") == [("deepseek/deepseek-v4", "DeepSeek - V4")]
    assert surface.scroll_position == 7


def test_card_renderer_closes_a_rectangular_border() -> None:
    transcript = ui.Transcript()
    transcript.add_message("agent", "ChatGPT", "你好！有什么我可以帮你的吗？", tokens=2371)

    rendered = "".join(text for _style, text in transcript.formatted())
    lines = [line for line in rendered.splitlines() if line]

    display_width = lambda line: sum(2 if east_asian_width(character) in {"F", "W"} else 1 for character in line)
    assert len({display_width(line) for line in lines}) == 1
    assert lines[0].startswith("╭") and lines[0].endswith("╮")
    assert all(line.startswith("│") and line.endswith("│") for line in lines[1:-1])
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")


def test_card_renderer_embeds_title_and_orders_token_before_time() -> None:
    transcript = ui.Transcript()
    card = transcript.add_message("agent", "ChatGPT", "Done", tokens=2371)
    card.created_at = "12:34"

    rendered = "".join(text for _style, text in transcript.formatted())
    header = rendered.splitlines()[0]

    assert "╴ ChatGPT ╶" in header
    assert header.index("2371 tok") < header.index("12:34")


def test_card_renderer_wraps_long_reply_without_dropping_text() -> None:
    transcript = ui.Transcript()
    reply = "x" * 130
    transcript.add_message("agent", "ChatGPT", reply)

    fragments = transcript.formatted()
    body_lines = [text.rstrip() for style, text in fragments if style == "class:card-content"]

    assert len(body_lines) == 3
    assert "".join(body_lines) == reply


def test_transcript_and_composer_use_the_terminal_default_background() -> None:
    assert ui.TERMINAL_STYLE.get_attrs_for_style_str("class:composer").bgcolor == ""
    assert ui.TERMINAL_STYLE.get_attrs_for_style_str("class:card-content").bgcolor == ""


def test_terminal_follows_the_latest_transcript_content() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.output.vertical_scroll = 0

    terminal.add_message("agent", "ChatGPT", "new reply")

    assert terminal.output.vertical_scroll == ui.LATEST_SCROLL


def test_secret_prompt_interruption_does_not_mask_later_input(monkeypatch) -> None:
    calls = []

    class Session:
        def prompt(self, _message, **kwargs):
            calls.append(kwargs.get("is_password"))
            if kwargs.get("is_password"):
                raise KeyboardInterrupt
            return "visible input"

    monkeypatch.setattr(ui, "prompt_session", Session())

    with pytest.raises(KeyboardInterrupt):
        ui.prompt_secret("API key")

    assert ui.prompt_text("Model") == "visible input"
    assert calls == [True, False]


def test_select_accepts_a_unique_search_term(monkeypatch) -> None:
    class Session:
        def prompt(self, _message, **_kwargs):
            return "pro"

    monkeypatch.setattr(ui, "prompt_session", Session())

    assert ui.prompt_select("Model", (
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    )) == "deepseek-v4-pro"


def test_select_rejects_an_ambiguous_search_term(monkeypatch) -> None:
    class Session:
        def prompt(self, _message, **_kwargs):
            return "deepseek"

    monkeypatch.setattr(ui, "prompt_session", Session())

    with pytest.raises(ValueError, match="ambiguous"):
        ui.prompt_select("Model", (
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ))
