"""Rich terminal output for song-eater."""

from pathlib import Path

from rich.console import Console

console = Console()


def show_listening(device_name: str) -> None:
    console.print(f"\n[bold cyan]song-eater[/] listening on [bold]{device_name}[/]...")
    console.print("  Press Ctrl+C to stop.\n")


def show_recording(track_num: int) -> None:
    console.print(f"[yellow]>[/] Recording track {track_num}...")


def show_identifying() -> None:
    console.print("  [dim]Identifying...[/]")


def show_saved(track_num: int, mp3_path: Path, metadata: dict) -> None:
    artist = metadata.get("artist", "Unknown")
    title = metadata.get("title", "Unknown")
    console.print(
        f"[green]OK[/] Track {track_num}: "
        f"[bold]{artist}[/] - [bold]{title}[/]  ->  {mp3_path.name}"
    )


def show_error(msg: str) -> None:
    console.print(f"[red]Error:[/] {msg}")


def show_done(total: int) -> None:
    console.print(f"\n[bold cyan]Done.[/] Saved {total} track{'s' if total != 1 else ''}.")
