import pytest
from collections import deque
from types import SimpleNamespace
from unicodedata import east_asian_width

from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.data_structures import Point

from penhin.cli import ui


def test_status_line_uses_the_active_runtime_context_window(monkeypatch) -> None:
    from penhin.runtime import runtime_manager

    monkeypatch.setattr(ui, "status_context", SimpleNamespace(messages=[]))
    monkeypatch.setattr(runtime_manager, "current", lambda: SimpleNamespace(context_window=200_000))
    monkeypatch.setattr(runtime_manager, "configured_provider", lambda: "openai")
    monkeypatch.setattr(runtime_manager, "status", lambda: SimpleNamespace(model="gpt-test"))
    monkeypatch.setattr("penhin.infrastructure.config.get_permission_mode", lambda: "default")

    assert "0.0% · 0.0k / 200k" in ui._status_line()


def test_status_line_keeps_the_entire_model_name_within_the_toolbar(monkeypatch) -> None:
    from penhin.runtime import runtime_manager

    monkeypatch.setattr(ui, "status_context", SimpleNamespace(messages=[]))
    monkeypatch.setattr(runtime_manager, "current", lambda: SimpleNamespace(context_window=200_000))
    monkeypatch.setattr(runtime_manager, "configured_provider", lambda: "openai")
    monkeypatch.setattr(runtime_manager, "status", lambda: SimpleNamespace(model="luna"))
    monkeypatch.setattr("penhin.infrastructure.config.get_permission_mode", lambda: "default")
    monkeypatch.setattr(ui, "get_terminal_size", lambda _fallback: SimpleNamespace(columns=100))

    toolbar = f"  {ui._status_line()}  "

    assert toolbar.endswith("openai/luna  ")
    assert ui._terminal_width(toolbar) <= 100


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


def test_empty_info_does_not_create_a_system_card(monkeypatch) -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    monkeypatch.setattr(ui, "active_terminal", terminal)

    ui.print_info("")

    assert terminal.cards == []


def test_error_and_warning_use_distinct_diagnostic_cards(monkeypatch) -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    monkeypatch.setattr(ui, "active_terminal", terminal)

    ui.print_error("request failed")
    ui.print_warning("using cached result")

    assert [(card.kind, card.name, card.color) for card in terminal.cards] == [
        ("error", "Error", "#f87171"),
        ("warning", "Warning", "#fbbf24"),
    ]


def test_terminal_structured_output_uses_indented_labels_without_json_braces(monkeypatch) -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    monkeypatch.setattr(ui, "active_terminal", terminal)

    ui.print_json({"main": {"enabled": True, "retry_after": 12}})

    assert terminal.cards[-1].content == "main\n  enabled: true\n  retry_after: 12"


def test_terminal_follows_the_latest_transcript_content() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.output.vertical_scroll = 0

    terminal.add_message("agent", "ChatGPT", "new reply")

    assert terminal.output.vertical_scroll == ui.LATEST_SCROLL


def test_terminal_scrolls_to_the_latest_rendered_line() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.add_message("agent", "ChatGPT", "\n".join(f"line {index}" for index in range(30)))

    content = terminal.output.content.create_content(width=60, height=100)
    terminal.output._scroll(content, width=60, height=10)

    assert content.cursor_position.y == content.line_count - 1
    assert terminal.output.vertical_scroll > 0


def test_terminal_keeps_a_manual_transcript_scroll_position() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.add_message("agent", "ChatGPT", "\n".join(f"line {index}" for index in range(30)))
    initial = terminal.output.content.create_content(width=60, height=100)
    terminal.output._scroll(initial, width=60, height=10)

    terminal.output.vertical_scroll -= 10
    after_manual_scroll = terminal.output.content.create_content(width=60, height=100)
    terminal.output._scroll(after_manual_scroll, width=60, height=10)

    assert terminal.output.vertical_scroll == 14


def test_transcript_scrollbar_click_moves_the_viewport() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.output.render_info = SimpleNamespace(content_height=34, window_height=10, get_height_for_line=lambda _line: 1)
    scrollbar = terminal.output.right_margins[0]

    scrollbar._mouse_handler(MouseEvent(Point(x=0, y=8), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()))

    assert terminal.output.vertical_scroll > 0


def test_transcript_scrollbar_forwards_wheel_events() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    terminal.output.vertical_scroll = 14
    terminal.output.render_info = SimpleNamespace(
        content_height=34,
        window_height=10,
        cursor_position=Point(x=0, y=14),
        configured_scroll_offsets=SimpleNamespace(top=0, bottom=0),
    )
    scrollbar = terminal.output.right_margins[0]

    scrollbar._mouse_handler(MouseEvent(Point(x=0, y=5), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()))

    assert terminal.output.vertical_scroll == 15


def test_transcript_scrollbar_uses_rendered_height_for_thumb_size() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    scrollbar = terminal.output.right_margins[0]

    class RenderInfo:
        content_height = 40
        window_height = 10
        displayed_lines = list(range(10))
        vertical_scroll = 10

        @staticmethod
        def get_height_for_line(_line: int) -> int:
            return 3

    fragments = scrollbar.create_margin(RenderInfo(), width=1, height=10)
    thumb_rows = [index for index, (style, text, *_rest) in enumerate(fragments) if text == " " and "scrollbar.button" in style]

    assert len(thumb_rows) == 1


def test_down_arrow_moves_forward_through_composer_history() -> None:
    terminal = ui.TerminalInterface(lambda _message: None)
    buffer = terminal.composer.buffer
    buffer._working_lines = deque(["first", "second", ""])
    buffer.working_index = 2
    event = SimpleNamespace(current_buffer=buffer)

    terminal.app.key_bindings.get_bindings_for_keys((Keys.Up,))[-1].handler(event)
    terminal.app.key_bindings.get_bindings_for_keys((Keys.Down,))[-1].handler(event)

    assert buffer.text == ""


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


def test_selection_surface_replaces_status_with_keyboard_help() -> None:
    terminal = object.__new__(ui.TerminalInterface)
    terminal._selection = ui.SelectionSurface(
        "Session tree",
        (("entry", "entry"),),
        ui.Queue(maxsize=1),
        scroll_position=0,
    )

    assert "↑/↓ move · Enter confirm · Esc cancel · type to filter" in terminal._bottom_toolbar()[0][1]
