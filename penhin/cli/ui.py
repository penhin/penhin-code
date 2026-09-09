import json
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue
from shutil import get_terminal_size
from threading import Lock
from typing import Callable
from unicodedata import east_asian_width

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from penhin.infrastructure.observability import cli_status_line


console = Console()
prompt_session = None
restore_queued_prompts = None
status_context = None
status_queue = None
active_terminal = None
LATEST_SCROLL = 1_000_000


TERMINAL_STYLE = Style.from_dict({
    "prompt": "bold #67e8f9",
    "prompt-label": "#64748b",
    "card-content": "#e2e8f0",
    "composer": "#e2e8f0",
    "composer-frame": "",
    "composer-frame.border": "#475569",
    "composer.border": "#475569",
    "bottom-toolbar": "bg:#16181d #94a3b8",
    "completion-menu": "bg:#111827 #cbd5e1",
    "completion-menu.completion": "bg:#111827 #cbd5e1",
    "completion-menu.completion.current": "bg:#0e7490 #ffffff bold",
    "completion-menu.meta.completion": "bg:#111827 #64748b",
    "completion-menu.meta.completion.current": "bg:#0e7490 #dbeafe",
    "scrollbar.background": "bg:#334155",
    "scrollbar.button": "bg:#67e8f9 #0f172a",
})


IDENTITY_COLORS = {
    "chatgpt": "#f8fafc",
    "openai": "#f8fafc",
    "deepseek": "#3b82f6",
    "claude code": "#f97316",
    "claude": "#f97316",
    "system": "#94a3b8",
    "you": "#67e8f9",
    "penhin": "#22c55e",
}


def _terminal_width(text: str) -> int:
    return sum(2 if east_asian_width(character) in {"F", "W"} else 1 for character in text)


def _fit_terminal_width(text: str, width: int) -> str:
    fitted: list[str] = []
    used = 0
    for character in text:
        character_width = _terminal_width(character)
        if used + character_width > width:
            break
        fitted.append(character)
        used += character_width
    return "".join(fitted) + " " * (width - used)


def _wrap_terminal_width(text: str, width: int) -> list[str]:
    """Soft-wrap text by terminal display width without discarding characters."""
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for character in text:
        character_width = _terminal_width(character)
        if current and used + character_width > width:
            lines.append("".join(current))
            current, used = [], 0
        current.append(character)
        used += character_width
    lines.append("".join(current))
    return lines


def _card_header_parts(card: "MessageCard", width: int) -> tuple[str, str, str, str]:
    """Build a fieldset-like header with the title breaking the top border."""
    usage = " " + (f"{card.tokens} tok  " if card.tokens is not None else "") + card.created_at
    prefix = f"╭──╴ {card.name} ╶"
    suffix = " ─╮"
    border = "─" * max(1, width - _terminal_width(prefix) - _terminal_width(usage) - _terminal_width(suffix))
    return prefix, border, usage, suffix


@dataclass
class MessageCard:
    """A user-visible terminal transcript event."""

    kind: str
    name: str
    content: str
    color: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M"))
    tokens: int | None = None


@dataclass
class CardStream:
    transcript: "Transcript"
    card: MessageCard

    def write(self, text: str) -> None:
        with self.transcript._lock:
            self.card.content += text

    def finish(self, *, tokens: int | None = None) -> None:
        with self.transcript._lock:
            self.card.tokens = tokens


@dataclass
class Transcript:
    """Structured transcript state owned by the Terminal UI Surface."""

    cards: list[MessageCard] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def message_color(self, name: str) -> str:
        normalized = name.casefold()
        for identity, color in IDENTITY_COLORS.items():
            if identity in normalized:
                return color
        # A deterministic, readable fallback for custom Agents.
        palette = ("#a78bfa", "#f59e0b", "#14b8a6", "#ec4899")
        return palette[sum(ord(character) for character in normalized) % len(palette)]

    def add_message(self, kind: str, name: str, content: str, *, tokens: int | None = None) -> MessageCard:
        with self._lock:
            card = MessageCard(kind, name, content, self.message_color(name), tokens=tokens)
            self.cards.append(card)
            return card

    def start_stream(self, name: str) -> CardStream:
        return CardStream(self, self.add_message("agent", name, ""))

    def render(self) -> str:
        rendered: list[str] = []
        for card in self.cards:
            width = max(24, min(100, max((len(line) for line in card.content.splitlines() or [""]), default=0) + 4))
            prefix, border, usage, suffix = _card_header_parts(card, width)
            rendered.extend((
                prefix + border + usage + suffix,
                *[f"│ {line[:width - 4]:<{width - 4}} │" for line in (card.content.splitlines() or [""])],
                "╰" + "─" * (width - 2) + "╯",
                "",
            ))
        return "\n".join(rendered)

    def formatted(self) -> FormattedText:
        result = FormattedText()
        with self._lock:
            cards = tuple(self.cards)
        outer_width = 60
        for card in cards:
            if card.kind == "startup":
                lines = card.content.splitlines()
                for index, line in enumerate(lines[:3]):
                    result.append(("fg:#22d3ee bold" if index != 1 else "fg:#3b82f6 bold", line + "\n"))
                result.extend((("class:heading", "\n".join(lines[3:]) + "\n\n"),))
                continue
            prefix, border, usage, suffix = _card_header_parts(card, outer_width)
            result.extend((
                (f"fg:{card.color} bold", prefix + border),
                ("class:prompt-label", usage),
                (f"fg:{card.color} bold", suffix + "\n"),
            ))
            for line in card.content.splitlines() or [""]:
                for wrapped_line in _wrap_terminal_width(line, outer_width - 4):
                    content = _fit_terminal_width(wrapped_line, outer_width - 4)
                    result.extend(((f"fg:{card.color} bold", "│ "), ("class:card-content", content), (f"fg:{card.color} bold", " │\n")))
            result.extend(((f"fg:{card.color} bold", "╰" + "─" * (outer_width - 2) + "╯\n\n"),))
        return result


@dataclass
class SelectionSurface:
    """A temporary, keyboard-first page displayed in place of the transcript."""

    title: str
    options: tuple[tuple[str, str], ...]
    reply: Queue[str | None]
    scroll_position: int
    selected: int = 0

    def matching_options(self, query: str) -> list[tuple[str, str]]:
        normalized = query.casefold()
        return [option for option in self.options if not normalized or normalized in option[0].casefold() or normalized in option[1].casefold()]


def configure_status(context=None, queue=None) -> None:
    global status_context, status_queue
    status_context, status_queue = context, queue


def _status_line() -> str:
    if status_context is None:
        return cli_status_line()
    from penhin.agent.compaction import estimate_api_tokens
    used = estimate_api_tokens(status_context.messages)
    from penhin.infrastructure.config import get_permission_mode
    from penhin.runtime import AuthenticationRequired, runtime_manager
    provider = runtime_manager.configured_provider() or "not configured"
    model = runtime_manager.status().model or "not configured"
    try:
        context_window = runtime_manager.current().context_window
    except AuthenticationRequired:
        context_window = 128_000
    left = f"{used / context_window:.1%} · {used / 1000:.1f}k / {context_window / 1000:.0f}k"
    middle = get_permission_mode()
    right = f"{provider}/{model}"
    width = max(40, get_terminal_size((100, 24)).columns - 2)
    middle_start = max(len(left) + 2, (width - len(middle)) // 2)
    right_start = max(middle_start + len(middle) + 2, width - len(right))
    return left + " " * (middle_start - len(left)) + middle + " " * (right_start - middle_start - len(middle)) + right


class DraggableScrollbarMargin(ScrollbarMargin):
    """A high-contrast transcript scrollbar that supports clicking and dragging."""

    def __init__(self, window: Window) -> None:
        super().__init__(display_arrows=True)
        self.window = window
        self._dragging = False

    def create_margin(self, window_render_info, width: int, height: int):
        total_height, visible_height, scroll_offset = self._rendered_metrics(window_render_info)
        track_height = max(0, height - 2)
        maximum_offset = max(0, total_height - visible_height)
        if maximum_offset == 0:
            thumb_height, thumb_top = track_height, 0
        else:
            thumb_height = max(1, round(track_height * visible_height / total_height))
            thumb_top = round((track_height - thumb_height) * scroll_offset / maximum_offset)

        fragments = [("class:scrollbar.arrow", self.up_arrow_symbol), ("class:scrollbar", "\n")]
        for row in range(track_height):
            style = "class:scrollbar.button" if thumb_top <= row < thumb_top + thumb_height else "class:scrollbar.background"
            fragments.extend(((style, " ", self._mouse_handler), ("", "\n")))
        fragments.append(("class:scrollbar.arrow", self.down_arrow_symbol))
        return fragments

    def _rendered_metrics(self, info) -> tuple[int, int, int]:
        line_heights = [info.get_height_for_line(line) for line in range(info.content_height)]
        total_height = max(1, sum(line_heights))
        visible_height = min(info.window_height, total_height)
        preceding_height = sum(line_heights[:self.window.vertical_scroll])
        scroll_offset = min(total_height - visible_height, preceding_height + self.window.vertical_scroll_2)
        return total_height, visible_height, scroll_offset

    def _mouse_handler(self, event: MouseEvent):
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            return self.window._mouse_handler(event)
        if event.event_type == MouseEventType.MOUSE_DOWN:
            self._dragging = True
        elif event.event_type == MouseEventType.MOUSE_UP:
            self._dragging = False
            return None
        elif event.event_type != MouseEventType.MOUSE_MOVE or not self._dragging:
            return NotImplemented

        info = self.window.render_info
        if info is None:
            return NotImplemented
        total_height, visible_height, _scroll_offset = self._rendered_metrics(info)
        maximum = max(0, total_height - visible_height)
        track_height = max(1, info.window_height - 2)
        track_row = min(track_height - 1, max(0, event.position.y - 1))
        target_offset = round(maximum * track_row / max(1, track_height - 1))
        remaining = target_offset
        for line in range(info.content_height):
            line_height = info.get_height_for_line(line)
            if remaining < line_height:
                self.window.vertical_scroll = line
                self.window.vertical_scroll_2 = remaining
                break
            remaining -= line_height
        return None


class TranscriptWindow(Window):
    """A transcript window that routes mouse events from its scrollbar margin."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.scrollbar = DraggableScrollbarMargin(self)
        self.right_margins = [self.scrollbar]

    def _write_to_screen_at_index(self, screen, mouse_handlers, write_position, parent_style, erase_bg) -> None:
        super()._write_to_screen_at_index(screen, mouse_handlers, write_position, parent_style, erase_bg)

        margin_x = write_position.xpos + write_position.width - 1

        def handle_margin_event(event: MouseEvent):
            return self.scrollbar._mouse_handler(
                MouseEvent(
                    position=Point(x=0, y=event.position.y - write_position.ypos),
                    event_type=event.event_type,
                    button=event.button,
                    modifiers=event.modifiers,
                )
            )

        mouse_handlers.set_mouse_handler_for_range(
            x_min=margin_x,
            x_max=margin_x + 1,
            y_min=write_position.ypos,
            y_max=write_position.ypos + write_position.height,
            handler=handle_margin_event,
        )


class TerminalInterface:
    """A full-screen terminal with a scrollable transcript and pinned composer."""

    def __init__(self, submit: Callable[[str], None], completer=None, cycle_permission: Callable[[], None] | None = None) -> None:
        self._submit = submit
        self._cycle_permission = cycle_permission
        self._input_request: Queue[str] | None = None
        self._input_request_lock = Lock()
        self._output_lock = Lock()
        self._selection: SelectionSurface | None = None
        self._waiting: str | None = None
        self._waiting_cancelled = False
        self._output_cursor_line = 0
        self._follow_latest = False
        self.transcript = Transcript()
        self.output = TranscriptWindow(
            FormattedTextControl(self._main_panel, get_cursor_position=self._latest_output_cursor),
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self.composer = TextArea(
            multiline=False,
            completer=completer,
            complete_while_typing=True,
            history=InMemoryHistory(),
            prompt=FormattedText([("class:prompt", "❯ ")]),
            style="class:composer",
        )
        self._placeholder_processor = ConditionalProcessor(
            BeforeInput("ask or /command", style="class:prompt-label"),
            Condition(lambda: not self.composer.text),
        )
        self.composer.buffer.input_processors = [self._placeholder_processor]
        bindings = KeyBindings()

        def selection_options() -> list[tuple[str, str]]:
            return self._selection.matching_options(self.composer.text) if self._selection else []

        def finish_selection(value: str | None) -> None:
            selection = self._selection
            if selection is None:
                return
            self._selection = None
            self.output.vertical_scroll = selection.scroll_position
            self.composer.buffer.reset()
            self.composer.prompt = FormattedText([("class:prompt", "❯ ")])
            self.composer.buffer.input_processors = [self._placeholder_processor]
            selection.reply.put(value)
            self.app.invalidate()

        @bindings.add("enter")
        def submit_prompt(event) -> None:
            if self._selection is not None:
                options = selection_options()
                if options:
                    finish_selection(options[min(self._selection.selected, len(options) - 1)][0])
                return
            value = self.composer.text.strip()
            if not value:
                return
            self.composer.buffer.history.append_string(value)
            self.composer.buffer.reset()
            if self._input_request is not None:
                request, self._input_request = self._input_request, None
                self.composer.prompt = FormattedText([("class:prompt", "❯ ")])
                self.composer.buffer.input_processors = [self._placeholder_processor]
                request.put(value)
                self.app.invalidate()
                return
            self._submit(value)

        @bindings.add("c-q")
        def restore_queue(event) -> None:
            if restore_queued_prompts is None:
                return
            restored = restore_queued_prompts()
            if restored:
                self.composer.buffer.text = "\n\n".join(restored)
                self.composer.buffer.cursor_position = len(self.composer.buffer.text)
                self.app.invalidate()

        @bindings.add("c-c")
        def exit_terminal(event) -> None:
            self.app.exit()

        @bindings.add("up")
        def recall_previous(event) -> None:
            if self._selection is not None:
                self._selection.selected = max(0, self._selection.selected - 1)
                self.app.invalidate()
                return
            event.current_buffer.history_backward()

        @bindings.add("down")
        def select_next(event) -> None:
            if self._selection is None:
                event.current_buffer.history_forward()
                return
            options = selection_options()
            if options:
                self._selection.selected = (self._selection.selected + 1) % len(options)
            self.app.invalidate()

        @bindings.add("tab")
        def select_next_with_tab(event) -> None:
            if self._selection is None:
                return
            options = selection_options()
            if options:
                self._selection.selected = (self._selection.selected + 1) % len(options)
            self.app.invalidate()

        @bindings.add("s-tab")
        def select_previous(event) -> None:
            if self._selection is not None:
                options = selection_options()
                if options:
                    self._selection.selected = (self._selection.selected - 1) % len(options)
                self.app.invalidate()
                return
            if self._cycle_permission is not None:
                self._cycle_permission()
                self.app.invalidate()

        @bindings.add("escape")
        def cancel_selection(event) -> None:
            if self._selection is not None:
                finish_selection(None)
            elif self._waiting is not None:
                self._waiting_cancelled = True
                self.clear_temporary_surface()

        self.composer.buffer.on_text_changed += lambda _buffer: self.app.invalidate()

        @bindings.add("pageup")
        def scroll_output_up(event) -> None:
            self.output.vertical_scroll = max(0, self.output.vertical_scroll - 10)
            self.app.invalidate()

        @bindings.add("pagedown")
        def scroll_output_down(event) -> None:
            self.output.vertical_scroll += 10
            self.app.invalidate()

        status = Window(
            FormattedTextControl(lambda: [("class:bottom-toolbar", f"  {_status_line()}  ")]),
            height=1,
            style="class:bottom-toolbar",
        )
        self.app = Application(
            layout=Layout(
                HSplit([
                    self.output,
                    Frame(self.composer, style="class:composer-frame", height=3),
                    status,
                ]),
                focused_element=self.composer,
            ),
            key_bindings=bindings,
            style=TERMINAL_STYLE,
            full_screen=True,
            refresh_interval=0.25,
            mouse_support=True,
        )

    def run(self) -> None:
        self.app.run()

    def append(self, text: str) -> None:
        self.add_message("system", "System", text.rstrip())

    @property
    def cards(self) -> list[MessageCard]:
        return self.transcript.cards

    def message_color(self, name: str) -> str:
        return self.transcript.message_color(name)

    def add_message(self, kind: str, name: str, content: str, *, tokens: int | None = None) -> MessageCard:
        with self._output_lock:
            card = self.transcript.add_message(kind, name, content, tokens=tokens)
        self._refresh_transcript()
        return card

    def start_stream(self, name: str) -> CardStream:
        stream = self.transcript.start_stream(name)
        self._refresh_transcript()
        return stream

    def _refresh_transcript(self) -> None:
        self._follow_latest = True
        self.output.vertical_scroll = LATEST_SCROLL
        self.app.invalidate()

    def _latest_output_cursor(self) -> Point:
        """Anchor the transcript viewport to its final rendered line."""
        if self._follow_latest:
            self._follow_latest = False
            return Point(x=0, y=self._output_cursor_line)
        return Point(x=0, y=min(self._output_cursor_line, self.output.vertical_scroll))

    def _main_panel(self) -> FormattedText:
        if self._waiting is not None:
            rows: FormattedText = [("class:heading", self._waiting + "\n")]
        elif self._selection is None:
            rows = self.transcript.formatted()
        else:
            options = self._selection.matching_options(self.composer.text)
            rows = [("class:heading", self._selection.title + "\n\n")]
            if not options:
                rows.append(("class:prompt-label", "No matching choices\n"))
            last_provider = ""
            for index, (value, label) in enumerate(options):
                provider = value.partition("/")[0]
                if provider and provider != last_provider:
                    rows.append(("class:prompt-label", f"\n{provider}\n"))
                    last_provider = provider
                marker = "› " if index == self._selection.selected else "  "
                style = "class:completion-menu.completion.current" if index == self._selection.selected else "class:composer"
                rows.append((style, marker + label + "\n"))

        self._output_cursor_line = max(0, sum(text.count("\n") for _style, text, *_ in rows))
        return rows

    def request_select(self, message: str, options: tuple[tuple[str, str], ...]) -> str:
        """Present a temporary selection page and restore the transcript on exit."""
        if not options:
            raise ValueError("selection requires at least one option")
        reply: Queue[str | None] = Queue(maxsize=1)
        self._selection = SelectionSurface(message, options, reply, self.output.vertical_scroll)
        self.composer.buffer.reset()
        self.composer.prompt = FormattedText([("class:prompt", "Filter: ")])
        self.composer.buffer.input_processors = []
        self.app.invalidate()
        value = reply.get()
        if value is None:
            raise KeyboardInterrupt
        return value

    def show_waiting(self, message: str) -> None:
        """Keep browser-auth progress out of the transcript."""
        self._waiting_cancelled = False
        self._waiting = message
        self.app.invalidate()

    def clear_temporary_surface(self) -> None:
        self._waiting = None
        self.app.invalidate()

    @property
    def temporary_surface_cancelled(self) -> bool:
        return self._waiting_cancelled

    def request_input(self, message: str, *, secret: bool = False) -> str:
        """Request a value through the pinned composer from a command worker."""
        with self._input_request_lock:
            request: Queue[str] = Queue(maxsize=1)
            self._input_request = request
            suffix = " (hidden)" if secret else ""
            self.composer.prompt = FormattedText([("class:prompt", f"{message}{suffix}: ")])
            self.composer.buffer.input_processors = [PasswordProcessor()] if secret else []
            self.app.invalidate()
            return request.get()


def activate_terminal(terminal: TerminalInterface, context, queue) -> None:
    global active_terminal
    active_terminal = terminal
    configure_status(context, queue)


def deactivate_terminal() -> None:
    global active_terminal
    active_terminal = None
    configure_status()


def _append_terminal(text: str) -> bool:
    if active_terminal is None:
        return False
    if text.startswith("Penhin Code"):
        active_terminal.add_message(
            "startup", "Penhin",
            "\n".join(("  ▄████▄", " ▐█ ◉ █▌", "  ▀█▁█▀", text.strip())),
        )
        return True
    active_terminal.append(text)
    return True


def _add_terminal(kind: str, name: str, text: str, *, tokens: int | None = None) -> bool:
    if active_terminal is None:
        return False
    active_terminal.add_message(kind, name, text, tokens=tokens)
    return True


def _request_terminal_input(message: str, *, secret: bool = False) -> str | None:
    if active_terminal is None:
        return None
    return active_terminal.request_input(message, secret=secret)


def clear_temporary_surface() -> None:
    if active_terminal is not None:
        active_terminal.clear_temporary_surface()


def temporary_surface_cancelled() -> bool:
    return active_terminal is not None and active_terminal.temporary_surface_cancelled


def get_prompt_session() -> PromptSession:
    global prompt_session
    if prompt_session is None:
        prompt_session = PromptSession(
            bottom_toolbar=lambda: [("class:bottom-toolbar", f"  {_status_line()}  ")],
            reserve_space_for_menu=8,
            complete_while_typing=True,
            style=Style.from_dict({
                "prompt": "bold #67e8f9",
                "prompt-label": "#64748b",
                "bottom-toolbar": "bg:#172033 #94a3b8",
                "completion-menu": "bg:#111827 #cbd5e1",
                "completion-menu.completion": "bg:#111827 #cbd5e1",
                "completion-menu.completion.current": "bg:#0e7490 #ffffff bold",
                "completion-menu.meta.completion": "bg:#111827 #64748b",
                "completion-menu.meta.completion.current": "bg:#0e7490 #dbeafe",
                "scrollbar.background": "bg:#111827",
                "scrollbar.button": "bg:#334155",
            }),
        )
    return prompt_session


def prompt_input(prompt: str = "❯ ", completer=None) -> str:
    bindings = KeyBindings()

    @bindings.add("c-q")
    def restore_queue(event) -> None:
        if restore_queued_prompts is None:
            return
        restored = restore_queued_prompts()
        if restored:
            event.current_buffer.text = "\n\n".join(restored)
            event.current_buffer.cursor_position = len(event.current_buffer.text)

    return get_prompt_session().prompt(
        FormattedText([("class:prompt", prompt)]),
        completer=completer,
        is_password=False,
        placeholder=FormattedText([("class:prompt-label", "ask or /command")]),
        key_bindings=bindings,
    )


async def prompt_text_async(message: str) -> str:
    terminal_value = _request_terminal_input(message)
    if terminal_value is not None:
        return terminal_value.strip()
    value = await get_prompt_session().prompt_async(f"{message}: ", is_password=False)
    return value.strip()


def prompt_secret(message: str) -> str:
    terminal_value = _request_terminal_input(message, secret=True)
    if terminal_value is not None:
        return terminal_value.strip()
    return get_prompt_session().prompt(f"{message}: ", is_password=True).strip()


def prompt_text(message: str) -> str:
    terminal_value = _request_terminal_input(message)
    if terminal_value is not None:
        return terminal_value.strip()
    return get_prompt_session().prompt(f"{message}: ", is_password=False).strip()


def prompt_confirm(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    terminal_value = _request_terminal_input(f"{message} {suffix}")
    answer = (
        terminal_value
        if terminal_value is not None
        else get_prompt_session().prompt(f"{message} {suffix} ", is_password=False)
    ).strip().lower()
    return answer in ({"", "y", "yes"} if default else {"y", "yes"})


def prompt_select(message: str, options: tuple[tuple[str, str], ...]) -> str:
    if active_terminal is not None:
        return active_terminal.request_select(message, options)
    else:
        console.print(message, style="cyan")
        for index, (_value, label) in enumerate(options, 1):
            console.print(f"  {index}. {label}")
        console.print()
        answer = get_prompt_session().prompt("Choose (number or search): ", is_password=False).strip()
    try:
        index = int(answer) - 1
    except ValueError:
        query = answer.casefold()
        exact = [value for value, label in options if query in {value.casefold(), label.casefold()}]
        if len(exact) == 1:
            return exact[0]
        matches = [value for value, label in options if query and (query in value.casefold() or query in label.casefold())]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("ambiguous selection; enter a more specific search or its number")
        raise ValueError("invalid selection")
    if 0 <= index < len(options):
        return options[index][0]
    raise ValueError("invalid selection")


def print_welcome(*, version: str, api: str, model: str, workspace: str) -> None:
    """Render the compact first-turn identity block shown by terminal coding agents."""
    if _append_terminal(
        f"Penhin Code v{version}\n{model}  ·  API: {api}\n{workspace}\n\n"
    ):
        return
    mark = Text()
    for line, style in [
        ("  ▄████▄", "bold cyan"),
        (" ▐█ ◉ █▌", "bold blue"),
        ("  ▀█▁█▀", "bold cyan"),
    ]:
        mark.append(line + "\n", style=style)
    title = Text.assemble(("Penhin Code", "bold"), (f" v{version}", "dim"))
    details = Text.assemble(
        (f"{model}", "bold white"), ("  ·  ", "dim"), (f"API: {api}", "dim"), ("\n", ""),
        (workspace, "dim"),
    )
    console.print()
    console.print(Columns([mark, Group(title, details)], padding=(0, 2), expand=False))
    console.print()


def _message_panel(title: str, text: str, color: str) -> Panel:
    return Panel(
        Text(text, style="white", no_wrap=False, overflow="fold"),
        title=Text(title, style=f"bold {color}"),
        title_align="left",
        border_style=color,
        padding=(0, 1),
        expand=True,
    )


@dataclass
class AssistantStream:
    chunks: list[str] = field(default_factory=list)
    live: Live | None = None
    terminal_stream: CardStream | None = None

    def start(self) -> None:
        if active_terminal is not None:
            from penhin.runtime import runtime_manager
            provider = runtime_manager.configured_provider() if runtime_manager.available() else "penhin"
            name = {
                "openai": "ChatGPT", "openai-codex": "ChatGPT", "anthropic": "Claude Code",
                "deepseek": "DeepSeek",
            }.get(provider, provider.title())
            self.terminal_stream = active_terminal.start_stream(name)
            return
        console.print()
        self.live = Live(_message_panel("Penhin", "", "green"), console=console, refresh_per_second=12, transient=False)
        self.live.start()

    def write(self, text: str) -> None:
        self.chunks.append(text)
        if self.terminal_stream is not None:
            self.terminal_stream.write(text)
            if active_terminal is not None:
                active_terminal._refresh_transcript()
            return
        if self.live is not None:
            self.live.update(_message_panel("Penhin", "".join(self.chunks), "green"))

    def finish(self, *, tokens: int | None = None) -> None:
        if self.terminal_stream is not None:
            self.terminal_stream.finish(tokens=tokens)
            if active_terminal is not None:
                active_terminal._refresh_transcript()
            return
        if self.live is not None:
            self.live.stop()
            self.live = None
        elif active_terminal is not None:
            active_terminal.append("\n")


def start_assistant_message() -> AssistantStream:
    stream = AssistantStream()
    stream.start()
    return stream


def finish_stream(stream: AssistantStream | None = None, *, tokens: int | None = None) -> None:
    if stream is not None:
        stream.finish(tokens=tokens)
        return
    if not _append_terminal("\n"):
        console.print()


def print_user_message(message: str) -> None:
    if _add_terminal("user", "You", message):
        return
    console.print()
    console.print(_message_panel("You", message, "cyan"))


def print_auth_url(url: str, instructions: str = "") -> None:
    if active_terminal is not None:
        active_terminal.show_waiting("Waiting for browser authorization…\n\nBrowser opened. Press Esc to cancel.\n" + (instructions or "Complete authorization in your browser."))
        return
    if _append_terminal(
        "\nOpen this link to continue authentication:\n"
        f"{url}\nCtrl/Cmd+click to open, or copy it into a browser.\n"
        f"{instructions}\n"
    ):
        return
    console.print()
    console.print(Text("Open this link to continue authentication:", style="bold cyan"))
    link = Text(url, style="bright_blue")
    link.stylize(f"link {url}")
    console.print(link)
    console.print(Text("Ctrl/Cmd+click to open, or copy it into a browser.", style="dim"))
    if instructions:
        console.print(Text(instructions, style="yellow"))
    console.print()


def print_device_code(verification_uri: str, user_code: str) -> None:
    if active_terminal is not None:
        active_terminal.show_waiting(f"Waiting for device authorization…\n\nCode: {user_code}")
        return
    if _append_terminal(
        "\nOpen this link to continue authentication:\n"
        f"{verification_uri}\nEnter code: {user_code}\n"
    ):
        return
    console.print()
    console.print(Text("Open this link to continue authentication:", style="bold cyan"))
    link = Text(verification_uri, style="bright_blue")
    link.stylize(f"link {verification_uri}")
    console.print(link)
    console.print(Text(f"Enter code: {user_code}", style="bold yellow"))
    console.print()


def print_info(message: str) -> None:
    from penhin.auth.secrets import redact_text
    if _add_terminal("system", "System", redact_text(message)):
        return
    console.print(Text(redact_text(message), style="cyan"))


def print_error(message: str) -> None:
    from penhin.auth.secrets import redact_text
    if _add_terminal("system", "System", f"Error: {redact_text(message)}"):
        return
    console.print(Text(redact_text(message), style="red"))


def print_json(data: object) -> None:
    if _add_terminal("system", "System", json.dumps(data, ensure_ascii=False, indent=2)):
        return
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    
