"""Rich TUI for song-eater — stable alternate-screen display with inline editing."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


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

    # Metadata detail line (populated during recording)
    meta_album: str = ""
    meta_track: str = ""       # e.g. "3" or "3/12"
    meta_disc: str = ""
    meta_year: str = ""
    meta_itunes: str = ""      # "iTunes ✓" | "iTunes ✗" | ""

    completed: list[CompletedTrack] = field(default_factory=list)
    error: str | None = None
    disk_free_gb: float | None = None   # updated periodically by main loop

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
    # Always returns exactly 2 lines so layout doesn't shift.
    if state.phase == "recording":
        elapsed = time.monotonic() - state.record_start
        time_str = _fmt_time(elapsed)
        if state.expected_duration > 0:
            time_str += f" / {_fmt_time(state.expected_duration)}"
        txt = Text(f"  ● Recording track {state.current_track}  [{time_str}]\n", style="bold yellow")
        if state.expected_duration > 0:
            label = "  Track".ljust(_LABEL_WIDTH)
            txt.append(label, style="dim")
            txt.append_text(_progress_bar(elapsed / state.expected_duration, bar_width))
        return txt
    if state.phase == "waiting":
        txt = Text("  Waiting for audio…\n", style="dim italic")
    elif state.phase == "identifying":
        txt = Text(f"  ◌ Identifying track {state.current_track}…\n", style="bold magenta")
    elif state.phase == "saving":
        txt = Text(f"  ◌ Saving track {state.current_track}…\n", style="bold blue")
    else:
        txt = Text(f"  {state.phase}\n", style="dim")
    return txt


# ---------------------------------------------------------------------------
# Scrollable track table
# ---------------------------------------------------------------------------

def _track_table(state: TUIState) -> Table:
    """Always returns a Table with exactly VISIBLE_ROWS data rows (padded if needed)."""
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

    # Compute which slice to show
    if total <= vis:
        start = 0
    elif state.scroll_pinned:
        start = total - vis
        state.scroll_offset = start
    else:
        state.scroll_offset = max(0, min(state.scroll_offset, total - vis))
        start = state.scroll_offset

    end = min(start + vis, total)

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

    # Pad with empty rows so the table height is always consistent
    shown = end - start
    for _ in range(vis - shown):
        tbl.add_row("", "", "", "")

    return tbl


# ---------------------------------------------------------------------------
# Count rendered lines of a list of Rich renderables
# ---------------------------------------------------------------------------

def _count_lines(parts: list, console: Console) -> int:
    """Render parts into a string and count newlines. Cheap and accurate."""
    # Use a temporary console to measure without printing
    from io import StringIO
    buf = StringIO()
    temp = Console(file=buf, width=console.width, force_terminal=True)
    for part in parts:
        temp.print(part)
    return buf.getvalue().count("\n")


# ---------------------------------------------------------------------------
# Full render
# ---------------------------------------------------------------------------

def build_renderable(state: TUIState, console: Console | None = None):
    """Build the full Rich renderable from current state."""

    term_height = (console.height if console else 0) or 24
    term_width = (console.width if console else 0) or 80

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
    bar_width = max(20, term_width - 26)

    meter = Text()
    label = "  Level".ljust(_LABEL_WIDTH)
    meter.append(label, style="dim")
    meter.append_text(_vu_bar(state.rms_level, bar_width))

    status = _status_line(state, bar_width)

    # Song name / error (always reserve the line so layout doesn't shift)
    if state.early_id_result:
        song_text = Text()
        song_text.append("♫ ", style="green")
        song_text.append(state.early_id_result, style="bold white")
        song_line = Padding(song_text, (0, 0, 0, 2))
    else:
        song_line = Text()

    # Metadata detail line
    meta_parts = []
    if state.meta_album:
        meta_parts.append(("Album: ", "dim"))
        meta_parts.append((state.meta_album, "white"))
    if state.meta_track:
        meta_parts.append(("  Track: ", "dim"))
        meta_parts.append((state.meta_track, "white"))
    if state.meta_disc:
        meta_parts.append(("  Disc: ", "dim"))
        meta_parts.append((state.meta_disc, "white"))
    if state.meta_year:
        meta_parts.append(("  Year: ", "dim"))
        meta_parts.append((state.meta_year, "white"))
    if state.meta_itunes:
        meta_parts.append(("  ", "dim"))
        if "✓" in state.meta_itunes:
            meta_parts.append((state.meta_itunes, "green"))
        else:
            meta_parts.append((state.meta_itunes, "dim red"))
    if meta_parts:
        meta_text = Text()
        for content, style in meta_parts:
            meta_text.append(content, style=style)
        meta_line = Padding(meta_text, (0, 0, 0, 4))
    else:
        meta_line = Text()

    if state.error:
        err_text = Text()
        err_text.append(f"✗ {state.error}", style="bold red")
        error_line = Padding(err_text, (0, 0, 0, 2))
    else:
        error_line = Text()

    # Disk space warning
    if state.disk_free_gb is not None and state.disk_free_gb < 5.0:
        disk_text = Text()
        if state.disk_free_gb < 1.0:
            disk_text.append(f"⚠ Disk critically low: {state.disk_free_gb:.1f} GB free", style="bold red")
        else:
            disk_text.append(f"⚠ Disk space low: {state.disk_free_gb:.1f} GB free", style="yellow")
        disk_line = Padding(disk_text, (0, 0, 0, 2))
    else:
        disk_line = Text()

    # The top section: everything above the tracks panel
    top_parts = [
        header,
        info,
        Text(""),
        meter,
        Text(""),
        status,
        Text(""),
        song_line,
        meta_line,
        error_line,
        disk_line,
        Text(""),
    ]

    # Footer
    total = len(state.completed)
    count_hint = f"  {total} track{'s' if total != 1 else ''}  │" if total else ""
    footer = Text(f" {count_hint}  ↑↓ scroll  │  Ctrl+C quit", style="dim")

    # Measure the top section + footer to compute remaining space for tracks.
    # Outer panel: border (2) + padding top/bottom (2) = 4 lines of chrome.
    # Tracks panel: border (2) + header row (1) = 3 lines of chrome.
    # Total fixed overhead = 4 + 3 = 7, plus the measured top/footer lines.
    top_lines = _count_lines(top_parts, console) if console else 12
    footer_lines = 1
    tracks_chrome = 3   # tracks panel border (2) + table header row (1)
    outer_chrome = 4    # outer panel border (2) + padding (2)

    available = term_height - outer_chrome - top_lines - footer_lines - tracks_chrome - 1
    state.VISIBLE_ROWS = max(3, available)

    track_content = _track_table(state)
    tracks_height = state.VISIBLE_ROWS + tracks_chrome

    parts = list(top_parts)
    parts.append(Panel(
        track_content,
        title="Tracks",
        border_style="green" if state.completed else "dim",
        padding=(0, 1),
        height=tracks_height,
    ))
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
