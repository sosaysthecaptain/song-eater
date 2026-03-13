"""Rich TUI for song-eater — stable alternate-screen display with inline editing."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class CompletedTrack:
    number: int
    artist: str
    title: str
    filename: str
    discarded: bool = False
    discard_reason: str = ""


@dataclass
class TUIState:
    """Mutable state bag that the render function reads."""

    source_name: str = ""
    output_dir: str = "."
    rms_level: float = 0.0          # raw RMS (not scaled)

    # "waiting" | "recording" | "identifying" | "saving"
    phase: str = "waiting"
    current_track: int = 0
    record_start: float = 0.0       # time.monotonic()

    early_id_result: str | None = None
    expected_duration: float = 0.0   # seconds, from Now Playing

    completed: list[CompletedTrack] = field(default_factory=list)
    error: str | None = None

    # -- Scroll --
    scroll_offset: int = 0
    scroll_pinned: bool = True    # True = auto-scroll to bottom

    VISIBLE_ROWS: int = 10


# ---------------------------------------------------------------------------
# VU meter (dB-scaled)
# ---------------------------------------------------------------------------

_VU_CHARS = "█"
_VU_BG = "░"
_DB_FLOOR = -50.0
_DB_CEIL = 0.0


def _vu_bar(rms: float, width: int = 40) -> Text:
    """Return a dB-scaled VU bar for raw *rms* value."""
    if rms <= 0:
        db = _DB_FLOOR
    else:
        db = max(_DB_FLOOR, min(_DB_CEIL, 20.0 * math.log10(rms)))

    fraction = (db - _DB_FLOOR) / (_DB_CEIL - _DB_FLOOR)
    filled = int(fraction * width)
    empty = width - filled

    bar = Text()
    green_end = int(width * 0.6)
    yellow_end = int(width * 0.8)
    for i in range(filled):
        if i < green_end:
            bar.append(_VU_CHARS, style="green")
        elif i < yellow_end:
            bar.append(_VU_CHARS, style="yellow")
        else:
            bar.append(_VU_CHARS, style="red")
    bar.append(_VU_BG * empty, style="dim")
    bar.append(f"  {db:+5.1f} dB", style="dim")
    return bar


# ---------------------------------------------------------------------------
# Phase / status line
# ---------------------------------------------------------------------------

def _fmt_time(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m:02d}:{s:02d}"


def _progress_bar(fraction: float, width: int = 40) -> Text:
    """Chunky progress bar."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    empty = width - filled
    pct = int(fraction * 100)

    bar = Text()
    bar.append("█" * filled, style="bold cyan")
    bar.append("░" * empty, style="dim")
    bar.append(f"  {pct:3d}%", style="bold white")
    return bar


_LABEL_WIDTH = 10  # "  Level  " / "  Track  " — consistent left margin for bars


def _status_line(state: TUIState, bar_width: int = 40) -> Text:
    if state.phase == "waiting":
        return Text("  Waiting for audio…", style="dim italic")
    if state.phase == "recording":
        elapsed = time.monotonic() - state.record_start
        time_str = _fmt_time(elapsed)
        if state.expected_duration > 0:
            time_str += f" / {_fmt_time(state.expected_duration)}"
        txt = Text(f"  ● Recording track {state.current_track}  [{time_str}]", style="bold yellow")
        if state.expected_duration > 0:
            txt.append("\n")
            label = "  Track".ljust(_LABEL_WIDTH)
            txt.append(label, style="dim")
            txt.append_text(_progress_bar(elapsed / state.expected_duration, bar_width))
        return txt
    if state.phase == "identifying":
        return Text(f"  ◌ Identifying track {state.current_track}…", style="bold magenta")
    if state.phase == "saving":
        return Text(f"  ◌ Saving track {state.current_track}…", style="bold blue")
    return Text(f"  {state.phase}", style="dim")


# ---------------------------------------------------------------------------
# Scrollable, editable track table
# ---------------------------------------------------------------------------

def _track_table(state: TUIState) -> Table | Text:
    if not state.completed:
        return Text("  No tracks captured yet.", style="dim")

    tbl = Table(
        show_header=True,
        header_style="bold",
        expand=True,
        padding=(0, 1),
        show_edge=False,
        show_lines=False,
    )
    tbl.add_column("#", width=4, justify="right")
    tbl.add_column("Artist", ratio=2, no_wrap=True)
    tbl.add_column("Title", ratio=3, no_wrap=True)
    tbl.add_column("File", ratio=3, style="dim", no_wrap=True)

    total = len(state.completed)
    vis = state.VISIBLE_ROWS

    if total <= vis:
        state.scroll_offset = 0
    elif state.scroll_pinned:
        state.scroll_offset = total - vis
    else:
        state.scroll_offset = max(0, min(state.scroll_offset, total - vis))

    start = state.scroll_offset
    end = min(start + vis, total)

    if start > 0:
        tbl.add_row("", Text(f"  ↑ {start} more", style="dim"), "", "")

    for idx in range(start, end):
        t = state.completed[idx]
        if t.discarded:
            style = "dim strike"
            tbl.add_row(
                Text(str(t.number), style=style),
                Text(t.artist, style=style),
                Text(t.title, style=style),
                Text(t.discard_reason, style="dim red"),
            )
        else:
            tbl.add_row(str(t.number), t.artist, t.title, t.filename)

    remaining = total - end
    if remaining > 0:
        tbl.add_row("", Text(f"  ↓ {remaining} more", style="dim"), "", "")

    return tbl


# ---------------------------------------------------------------------------
# Full render
# ---------------------------------------------------------------------------

def build_renderable(state: TUIState, console: Console | None = None):
    """Build the full Rich renderable from current state."""

    # Dynamically size the track table to fill the terminal
    term_height = (console.height if console else 0) or 24
    # Fixed chrome: outer panel border (2) + padding (2) + header + info +
    # blank + meter + blank + status + blank + song_line + error_line + blank +
    # tracks panel border (2) + tracks header row (1) + footer
    chrome = 2 + 2 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 2 + 1 + 1
    if state.phase == "recording" and state.expected_duration > 0:
        chrome += 1  # progress bar line
    # Scroll indicator rows
    total = len(state.completed)
    scroll_indicators = 0
    if total > 0:
        vis = max(3, term_height - chrome)
        if state.scroll_offset > 0:
            scroll_indicators += 1
        remaining_below = total - min(state.scroll_offset + vis, total)
        if remaining_below > 0:
            scroll_indicators += 1
    state.VISIBLE_ROWS = max(3, term_height - chrome - scroll_indicators)

    header = Text()
    header.append("  song-eater", style="bold cyan")
    header.append("  │  ", style="dim")
    header.append(state.source_name, style="bold white")

    info = Text()
    info.append("  Saving songs from ", style="dim")
    info.append(state.source_name, style="white")
    info.append(" to ", style="dim")
    info.append(state.output_dir, style="white")
    info.append(" as ", style="dim")
    info.append("192k MP3", style="white")

    # Compute bar width from terminal width
    # outer border (2) + padding (4) + label (10) + suffix (~10) = 26 chars overhead
    term_width = (console.width if console else 0) or 80
    bar_width = max(20, term_width - 26)

    meter = Text()
    label = "  Level".ljust(_LABEL_WIDTH)
    meter.append(label, style="dim")
    meter.append_text(_vu_bar(state.rms_level, bar_width))

    status = _status_line(state, bar_width)

    # Song name / error (always reserve the line so layout doesn't shift)
    song_line = Text()
    if state.early_id_result:
        song_line.append("  ♫ ", style="green")
        song_line.append(state.early_id_result, style="bold white")

    error_line = Text()
    if state.error:
        error_line.append(f"  ✗ {state.error}", style="bold red")

    parts: list[Text | Panel | Table] = [
        header,
        info,
        Text(""),
        meter,
        Text(""),
        status,
        Text(""),
        song_line,
        error_line,
        Text(""),
    ]

    track_content = _track_table(state)
    parts.append(Panel(
        track_content,
        title="Tracks",
        border_style="green" if state.completed else "dim",
        padding=(0, 1),
    ))

    # Footer
    scroll_hint = "  ↑↓ scroll  │" if total > state.VISIBLE_ROWS else ""
    footer = Text(f" {scroll_hint}  Ctrl+C quit", style="dim")

    parts.append(footer)

    return Panel(
        Group(*parts),
        border_style="cyan",
        padding=(1, 2),
        height=term_height,
    )


# ---------------------------------------------------------------------------
# Create a Live display on the alternate screen buffer
# ---------------------------------------------------------------------------

def make_live() -> Live:
    """Return a Live instance using the alternate screen for flicker-free rendering."""
    console = Console()
    return Live(
        Text("Starting…"),
        console=console,
        refresh_per_second=4,   # we also throttle manually on top of this
        screen=True,            # alternate screen buffer — no scrolling artifacts
        transient=False,
    )
