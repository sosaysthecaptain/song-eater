"""Rich TUI for song-eater using Live display and Layout."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# State model -- the CLI mutates this, the TUI renders it each tick
# ---------------------------------------------------------------------------

@dataclass
class CompletedTrack:
    number: int
    artist: str
    title: str
    filename: str


@dataclass
class TUIState:
    """Mutable state bag that the render function reads."""

    source_name: str = ""
    rms_level: float = 0.0          # 0.0 .. 1.0 (clamped)

    # "waiting" | "recording" | "identifying" | "saving"
    phase: str = "waiting"
    current_track: int = 0
    record_start: float = 0.0       # time.monotonic() when recording began

    early_id_result: str | None = None   # "Artist - Title" or None

    completed: list[CompletedTrack] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# VU meter
# ---------------------------------------------------------------------------

_VU_WIDTH = 50          # characters wide
_VU_CHARS = "█"
_VU_BG = "░"


def _vu_bar(level: float) -> Text:
    """Return a coloured horizontal VU bar for *level* (0..1)."""
    clamped = max(0.0, min(1.0, level))
    filled = int(clamped * _VU_WIDTH)
    empty = _VU_WIDTH - filled

    bar = Text()
    if filled > 0:
        # Green up to 60%, yellow 60-80%, red 80-100%
        green_end = int(_VU_WIDTH * 0.6)
        yellow_end = int(_VU_WIDTH * 0.8)
        for i in range(filled):
            if i < green_end:
                bar.append(_VU_CHARS, style="green")
            elif i < yellow_end:
                bar.append(_VU_CHARS, style="yellow")
            else:
                bar.append(_VU_CHARS, style="red")
    bar.append(_VU_BG * empty, style="dim")

    # dB label
    if clamped > 0:
        db = 20 * __import__("math").log10(clamped + 1e-10)
        bar.append(f"  {db:+5.1f} dB", style="dim")
    else:
        bar.append("  -inf dB", style="dim")
    return bar


# ---------------------------------------------------------------------------
# Phase / status line
# ---------------------------------------------------------------------------

def _status_line(state: TUIState) -> Text:
    if state.phase == "waiting":
        return Text("  Waiting for audio...", style="dim italic")
    if state.phase == "recording":
        elapsed = time.monotonic() - state.record_start
        mins, secs = divmod(int(elapsed), 60)
        txt = Text(f"  Recording track {state.current_track}... [{mins:02d}:{secs:02d}]", style="bold yellow")
        return txt
    if state.phase == "identifying":
        return Text(f"  Identifying track {state.current_track}...", style="bold magenta")
    if state.phase == "saving":
        return Text(f"  Saving track {state.current_track}...", style="bold blue")
    return Text(f"  {state.phase}", style="dim")


# ---------------------------------------------------------------------------
# Completed track log
# ---------------------------------------------------------------------------

def _track_table(state: TUIState) -> Table:
    tbl = Table(
        show_header=True,
        header_style="bold",
        expand=True,
        padding=(0, 1),
        show_edge=False,
    )
    tbl.add_column("#", width=4, justify="right")
    tbl.add_column("Artist", ratio=2)
    tbl.add_column("Title", ratio=3)
    tbl.add_column("File", ratio=3, style="dim")

    for t in state.completed:
        tbl.add_row(str(t.number), t.artist, t.title, t.filename)

    return tbl


# ---------------------------------------------------------------------------
# Full render
# ---------------------------------------------------------------------------

def build_renderable(state: TUIState):
    """Build the full Rich renderable from current state."""

    # -- Header --
    header_text = Text()
    header_text.append("  song-eater", style="bold cyan")
    header_text.append("  |  ", style="dim")
    header_text.append(f"Capturing from: {state.source_name}", style="bold white")

    # -- Level meter --
    meter = Text()
    meter.append("  Level: ", style="dim")
    meter.append_text(_vu_bar(state.rms_level))

    # -- Status --
    status = _status_line(state)

    # -- Early ID --
    early_id = Text()
    if state.early_id_result:
        early_id.append("  Detected: ", style="bold green")
        early_id.append(state.early_id_result, style="bold white")

    # -- Error --
    err = Text()
    if state.error:
        err.append(f"  Error: {state.error}", style="bold red")

    # -- Track log --
    if state.completed:
        track_panel = Panel(
            _track_table(state),
            title="Completed Tracks",
            border_style="green",
            padding=(0, 1),
        )
    else:
        track_panel = Text("  No tracks captured yet.", style="dim")

    # -- Footer --
    footer = Text("  Press Ctrl+C to stop", style="dim italic")

    # Compose vertically
    parts = [
        Text(""),
        header_text,
        Text(""),
        meter,
        Text(""),
        status,
    ]
    if state.early_id_result:
        parts.append(early_id)
    if state.error:
        parts.append(err)
    parts += [
        Text(""),
        track_panel,
        Text(""),
        footer,
        Text(""),
    ]

    return Panel(
        Group(*parts),
        border_style="cyan",
        title="[bold cyan]song-eater[/]",
        subtitle="[dim]v0.1.0[/]",
    )


# ---------------------------------------------------------------------------
# Convenience: create a Live context manager
# ---------------------------------------------------------------------------

def make_live() -> Live:
    """Return a configured Rich Live instance (caller manages the context)."""
    return Live(
        Text("Starting..."),
        refresh_per_second=4,
        screen=True,
        transient=False,
    )
