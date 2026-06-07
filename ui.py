import json

from rich.console import Console


console = Console()


def prompt_input(prompt: str = "penhin >> ") -> str:
    return console.input(f"[bold cyan]{prompt}[/bold cyan]")


def print_text(message: str) -> None:
    console.print(message)


def print_info(message: str) -> None:
    console.print(message, style="cyan")


def print_warning(message: str) -> None:
    console.print(message, style="yellow")


def print_error(message: str) -> None:
    console.print(message, style="red")


def print_json(data: object) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
