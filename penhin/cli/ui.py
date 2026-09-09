import json
from dataclasses import dataclass, field
from queue import Queue
from threading import Lock
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
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


TERMINAL_STYLE = Style.from_dict({
    "prompt": "bold #67e8f9",
    "prompt-label": "#64748b",
    "composer": "bg:#16181d #e2e8f0",
    "composer.border": "#475569",
    "bottom-toolbar": "bg:#16181d #94a3b8",
    "completion-menu": "bg:#111827 #cbd5e1",
    "completion-menu.completion": "bg:#111827 #cbd5e1",
    "completion-menu.completion.current": "bg:#0e7490 #ffffff bold",
    "completion-menu.meta.completion": "bg:#111827 #64748b",
    "completion-menu.meta.completion.current": "bg:#0e7490 #dbeafe",
    "scrollbar.background": "bg:#111827",
    "scrollbar.button": "bg:#334155",
})


def configure_status(context=None, queue=None) -> None:
    global status_context, status_queue
    status_context, status_queue = context, queue


def _status_line() -> str:
    if status_context is None:
        return cli_status_line()
    from penhin.agent.compaction import BLOCKING_THRESHOLD, estimate_api_tokens
    used = estimate_api_tokens(status_context.messages, status_context.collapse_keep_recent)
    queued = status_queue.size if status_queue is not None else 0
    queue_label = f"  ·  {queued} queued" if queued else ""
    return (
        f"{used / BLOCKING_THRESHOLD:.1%} · {used / 1000:.1f}k / "
        f"{BLOCKING_THRESHOLD / 1000:.0f}k (auto){queue_label}"
    )


class TerminalInterface:
    """A full-screen terminal with a scrollable transcript and pinned composer."""

    def __init__(self, submit: Callable[[str], None], completer=None) -> None:
        self._submit = submit
        self._input_request: Queue[str] | None = None
        self._input_request_lock = Lock()
        self._output_lock = Lock()
        self.output = TextArea(read_only=True, scrollbar=True, wrap_lines=True, focusable=False)
        self.composer = TextArea(
            multiline=False,
            completer=completer,
            complete_while_typing=True,
            prompt=FormattedText([("class:prompt", "❯ ")]),
            style="class:composer",
        )
        self._placeholder_processor = ConditionalProcessor(
            BeforeInput("ask or /command", style="class:prompt-label"),
            Condition(lambda: not self.composer.text),
        )
        self.composer.buffer.input_processors = [self._placeholder_processor]
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit_prompt(event) -> None:
            value = self.composer.text.strip()
            if not value:
                return
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

        @bindings.add("pageup")
        def scroll_output_up(event) -> None:
            self.output.window.vertical_scroll = max(0, self.output.window.vertical_scroll - 10)
            self.app.invalidate()

        @bindings.add("pagedown")
        def scroll_output_down(event) -> None:
            self.output.window.vertical_scroll += 10
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
                    Frame(self.composer, style="class:composer", height=3),
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
        with self._output_lock:
            updated = self.output.text + text
            self.output.buffer.set_document(Document(updated, cursor_position=len(updated)), bypass_readonly=True)
        self.app.invalidate()

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
    active_terminal.append(text)
    return True


def _request_terminal_input(message: str, *, secret: bool = False) -> str | None:
    if active_terminal is None:
        return None
    return active_terminal.request_input(message, secret=secret)


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
        _append_terminal("\n" + message + "\n")
        for index, (_value, label) in enumerate(options, 1):
            _append_terminal(f"  {index}. {label}\n")
        answer = active_terminal.request_input("Choose (number or search)").strip()
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

    def start(self) -> None:
        if _append_terminal("\nPenhin\n"):
            return
        console.print()
        self.live = Live(_message_panel("Penhin", "", "green"), console=console, refresh_per_second=12, transient=False)
        self.live.start()

    def write(self, text: str) -> None:
        self.chunks.append(text)
        if _append_terminal(text):
            return
        if self.live is not None:
            self.live.update(_message_panel("Penhin", "".join(self.chunks), "green"))

    def finish(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
        elif active_terminal is not None:
            active_terminal.append("\n")


def start_assistant_message() -> AssistantStream:
    stream = AssistantStream()
    stream.start()
    return stream


def finish_stream(stream: AssistantStream | None = None) -> None:
    if stream is not None:
        stream.finish()
        return
    if not _append_terminal("\n"):
        console.print()


def print_user_message(message: str) -> None:
    if _append_terminal(f"\nYou\n{message}\n"):
        return
    console.print()
    console.print(_message_panel("You", message, "cyan"))


def print_auth_url(url: str, instructions: str = "") -> None:
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
    if _append_terminal(f"{redact_text(message)}\n"):
        return
    console.print(Text(redact_text(message), style="cyan"))


def print_error(message: str) -> None:
    from penhin.auth.secrets import redact_text
    if _append_terminal(f"Error: {redact_text(message)}\n"):
        return
    console.print(Text(redact_text(message), style="red"))


def print_json(data: object) -> None:
    if _append_terminal(json.dumps(data, ensure_ascii=False, indent=2) + "\n"):
        return
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    
