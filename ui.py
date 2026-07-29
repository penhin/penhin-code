import json
from dataclasses import dataclass, field

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from observability import cli_status_line


console = Console()
prompt_session = None
prompt_completer = None


def set_prompt_completer(completer) -> None:
    global prompt_completer
    prompt_completer = completer


def get_prompt_session() -> PromptSession:
    global prompt_session
    if prompt_session is None:
        prompt_session = PromptSession(
            bottom_toolbar=lambda: [("class:bottom-toolbar", f"  {cli_status_line()}  ")],
            style=Style.from_dict({"bottom-toolbar": "bg:#262626 #b8c7d9"}),
        )
    return prompt_session


def prompt_input(prompt: str = "❯ ", completer=None) -> str:
    return get_prompt_session().prompt(
        ANSI(f"\x1b[1;36m{prompt}\x1b[0m"),
        completer=prompt_completer if completer is None else completer,
    )


def print_text(message: str) -> None:
    console.print(message)


def print_welcome(*, version: str, api: str, model: str, workspace: str) -> None:
    """Render the compact first-turn identity block shown by terminal coding agents."""
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
        console.print()
        self.live = Live(_message_panel("Penhin", "", "green"), console=console, refresh_per_second=12, transient=False)
        self.live.start()

    def write(self, text: str) -> None:
        self.chunks.append(text)
        if self.live is not None:
            self.live.update(_message_panel("Penhin", "".join(self.chunks), "green"))

    def finish(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None


def start_assistant_message() -> AssistantStream:
    stream = AssistantStream()
    stream.start()
    return stream


def print_stream_delta(text: str) -> None:
    """Compatibility fallback for non-streaming callers."""
    console.print(text, end="", highlight=False, markup=False)


def finish_stream(stream: AssistantStream | None = None) -> None:
    if stream is not None:
        stream.finish()
        return
    console.print()


def print_user_message(message: str) -> None:
    console.print("\n")
    console.print(_message_panel("You", message, "cyan"))


def print_info(message: str) -> None:
    console.print(message, style="cyan")


def print_warning(message: str) -> None:
    console.print(message, style="yellow")


def print_error(message: str) -> None:
    console.print(message, style="red")


def print_json(data: object) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    
