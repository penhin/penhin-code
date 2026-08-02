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

from penhin.infrastructure.observability import cli_status_line


console = Console()
prompt_session = None


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
        completer=completer,
        is_password=False,
    )


async def prompt_text_async(message: str) -> str:
    value = await get_prompt_session().prompt_async(f"{message}: ", is_password=False)
    return value.strip()


def prompt_secret(message: str) -> str:
    return get_prompt_session().prompt(f"{message}: ", is_password=True).strip()


def prompt_text(message: str) -> str:
    return get_prompt_session().prompt(f"{message}: ", is_password=False).strip()


def prompt_confirm(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = get_prompt_session().prompt(f"{message} {suffix} ", is_password=False).strip().lower()
    return answer in ({"", "y", "yes"} if default else {"y", "yes"})


def prompt_select(message: str, options: tuple[tuple[str, str], ...]) -> str:
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


def finish_stream(stream: AssistantStream | None = None) -> None:
    if stream is not None:
        stream.finish()
        return
    console.print()


def print_user_message(message: str) -> None:
    console.print()
    console.print(_message_panel("You", message, "cyan"))


def print_auth_url(url: str, instructions: str = "") -> None:
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
    console.print()
    console.print(Text("Open this link to continue authentication:", style="bold cyan"))
    link = Text(verification_uri, style="bright_blue")
    link.stylize(f"link {verification_uri}")
    console.print(link)
    console.print(Text(f"Enter code: {user_code}", style="bold yellow"))
    console.print()


def print_info(message: str) -> None:
    from penhin.auth.secrets import redact_text
    console.print(Text(redact_text(message), style="cyan"))


def print_error(message: str) -> None:
    from penhin.auth.secrets import redact_text
    console.print(Text(redact_text(message), style="red"))


def print_json(data: object) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    
