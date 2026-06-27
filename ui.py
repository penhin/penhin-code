import json

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console


console = Console()
prompt_session = None
prompt_completer = None


def set_prompt_completer(completer) -> None:
    global prompt_completer
    prompt_completer = completer


def get_prompt_session() -> PromptSession:
    global prompt_session
    if prompt_session is None:
        prompt_session = PromptSession()
    return prompt_session


def prompt_input(prompt: str = "penhin >> ", completer=None) -> str:
    return get_prompt_session().prompt(
        ANSI(f"\x1b[1;36m{prompt}\x1b[0m"),
        completer=prompt_completer if completer is None else completer,
    )


def print_text(message: str) -> None:
    console.print(message)


def print_stream_delta(text: str) -> None:
    console.print(text, end="", highlight=False, markup=False)


def finish_stream() -> None:
    console.print()


def print_info(message: str) -> None:
    console.print(message, style="cyan")


def print_warning(message: str) -> None:
    console.print(message, style="yellow")


def print_error(message: str) -> None:
    console.print(message, style="red")


def print_json(data: object) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    
