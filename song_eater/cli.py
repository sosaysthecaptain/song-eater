"""CLI entry point for song-eater with Rich TUI."""

from __future__ import annotations

import select
import sys
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path

import click
import numpy as np

from song_eater import display, export, identify, nowplaying, recorder


def _fmt_dur(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Non-blocking keyboard input
# ---------------------------------------------------------------------------

class _KeyReader:
    """Read single keypresses without blocking, using raw terminal mode."""

    def __init__(self):
        self._old_settings = None
        self._active = False

    def start(self):
        try:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            self._active = True
        except (termios.error, OSError):
            self._active = False

    def stop(self):
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except (termios.error, OSError):
                pass
            self._active = False

    def read_key(self) -> str | None:
        """Return a keypress if available, else None. Non-blocking."""
        if not self._active:
            return None
        try:
            if select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        ch2 = sys.stdin.read(1)
                        if ch2 == "[" and select.select([sys.stdin], [], [], 0.05)[0]:
                            ch3 = sys.stdin.read(1)
                            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3)
                    return "escape"
                if ch == "\r" or ch == "\n":
                    return "enter"
                if ch == "\x7f" or ch == "\x08":
                    return "backspace"
                if ch == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt
                if ch.isprintable():
                    return ch
        except (OSError, ValueError):
            pass
        return None


# ---------------------------------------------------------------------------
# Background Shazam identifier (for when Now Playing is unavailable)
# ---------------------------------------------------------------------------

class _ShazamIdentifier:
    """Fire-and-forget Shazam identification on a partial recording."""

    def __init__(self, audio: np.ndarray, sample_rate: int, state: display.TUIState):
        self._audio = audio
        self._sr = sample_rate
        self._state = state
        self._result: dict | None = None
        self._done = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            recorder.write_wav(tmp_path, self._audio, self._sr)
            result = identify.shazam_recognize(tmp_path)
            Path(tmp_path).unlink(missing_ok=True)
            if result.get("title", "Unknown") != "Unknown":
                self._result = result
                self._state.early_id_result = (
                    f"{result.get('artist', '?')} – {result.get('title', '?')}"
                )
        except Exception:
            pass
        finally:
            self._done = True

    @property
    def done(self) -> bool:
        return self._done

    @property
    def result(self) -> dict | None:
        return self._result


# ---------------------------------------------------------------------------
# Keyboard / editing logic
# ---------------------------------------------------------------------------

def _handle_key(key: str | None, state: display.TUIState, track_paths: dict[int, Path]) -> None:
    """Process a single keypress and mutate *state* accordingly."""
    if key is None:
        return

    if state.editing:
        _handle_edit_key(key, state, track_paths)
    else:
        _handle_nav_key(key, state)


def _handle_nav_key(key: str, state: display.TUIState) -> None:
    """Navigation mode: arrow keys, enter to edit, e to enter selection."""
    if not state.completed:
        return

    if key == "e" and state.selected_row < 0:
        # Enter selection mode on last track
        state.selected_row = len(state.completed) - 1
        state.selected_col = 0
        _ensure_visible(state)
        return

    if state.selected_row < 0:
        return  # not in selection mode

    if key == "up":
        state.selected_row = max(0, state.selected_row - 1)
        _ensure_visible(state)
    elif key == "down":
        state.selected_row = min(len(state.completed) - 1, state.selected_row + 1)
        _ensure_visible(state)
    elif key == "left":
        state.selected_col = 0
    elif key == "right":
        state.selected_col = 1
    elif key == "enter":
        # Start editing
        track = state.completed[state.selected_row]
        state.editing = True
        state.edit_buffer = track.artist if state.selected_col == 0 else track.title
    elif key == "escape":
        state.selected_row = -1
        state.editing = False


def _handle_edit_key(key: str, state: display.TUIState, track_paths: dict[int, Path]) -> None:
    """Edit mode: type to replace, enter to save, escape to cancel."""
    if key == "escape":
        state.editing = False
        return

    if key == "enter":
        # Save the edit
        track = state.completed[state.selected_row]
        mp3_path = track_paths.get(track.number)
        new_val = state.edit_buffer.strip()
        if not new_val:
            new_val = "Unknown"

        old_artist, old_title = track.artist, track.title

        if state.selected_col == 0:
            track.artist = new_val
        else:
            track.title = new_val

        # Re-tag and rename the MP3 if it exists
        if mp3_path and mp3_path.exists() and (track.artist != old_artist or track.title != old_title):
            try:
                from mutagen.id3 import ID3, TIT2, TPE1
                tags = ID3(str(mp3_path))
                tags.add(TIT2(encoding=3, text=track.title))
                tags.add(TPE1(encoding=3, text=track.artist))
                tags.save()

                new_filename = export._sanitize(f"{track.artist} - {track.title}.mp3")
                new_path = mp3_path.parent / new_filename
                if new_path != mp3_path and not new_path.exists():
                    mp3_path.rename(new_path)
                    track.filename = new_filename
                    track_paths[track.number] = new_path
            except Exception:
                pass  # best-effort re-tag

        state.editing = False
        return

    if key == "backspace":
        state.edit_buffer = state.edit_buffer[:-1]
    elif len(key) == 1 and key.isprintable():
        state.edit_buffer += key


def _ensure_visible(state: display.TUIState) -> None:
    """Adjust scroll offset so selected_row is visible."""
    if state.selected_row < state.scroll_offset:
        state.scroll_offset = state.selected_row
    elif state.selected_row >= state.scroll_offset + state.VISIBLE_ROWS:
        state.scroll_offset = state.selected_row - state.VISIBLE_ROWS + 1


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

HELP_TEXT = """\
Record audio from any app, split tracks on silence, identify via macOS Now
Playing (or Shazam fallback), and save as tagged MP3s.

\b
Examples:
  song-eater                             Capture from Chrome (default)
  song-eater -p Spotify                  Capture from Spotify
  song-eater -p Chrome -o ~/Music        Save MP3s to ~/Music
  song-eater -a "Pink Floyd" -A "DSOTM"  Manual mode (skip identification)

\b
Keyboard controls (during capture):
  e            Select tracks for editing
  ↑ ↓          Navigate track list
  ← →          Switch between Artist / Title columns
  Enter        Edit selected cell
  Esc          Cancel edit / deselect
  Ctrl+C       Stop recording and exit
"""


@click.command(help=HELP_TEXT)
@click.option(
    "--process", "-p", default="Chrome",
    help="App to capture audio from.",
    show_default=True,
)
@click.option(
    "--device", "-d", default=None,
    help="Audio input device name instead of app capture (e.g. 'BlackHole').",
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), default=None,
    help="Output directory for MP3 files. Default: current directory.",
)
@click.option(
    "--artist", "-a", default=None,
    help="Artist name — enables manual mode (skips identification).",
)
@click.option(
    "--album", "-A", default=None,
    help="Album name for manual mode.",
)
@click.option(
    "--threshold", "-t", type=float, default=0.01,
    help="RMS silence threshold for track splitting.",
    show_default=True,
)
@click.option(
    "--silence-duration", "-s", type=float, default=3.0,
    help="Seconds of silence required to split tracks.",
    show_default=True,
)
@click.option(
    "--sample-rate", type=int, default=48000,
    help="Sample rate in Hz.",
    show_default=True,
)
def main(process, device, output, artist, album, threshold, silence_duration, sample_rate):
    if output is None:
        output = Path.cwd()
    output.mkdir(parents=True, exist_ok=True)

    manual_mode = artist is not None
    has_nowplaying = nowplaying.is_available()

    # ------------------------------------------------------------------
    # Resolve capture source
    # ------------------------------------------------------------------
    if device:
        try:
            import sounddevice as sd
            device_index = recorder.find_device(device)
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)
        source_label = sd.query_devices(device_index)["name"]
        reader_kwargs = dict(device=device_index)
    else:
        source_label = process
        reader_kwargs = dict(process_name=process)

    # ------------------------------------------------------------------
    # Set up state
    # ------------------------------------------------------------------
    state = display.TUIState(source_name=source_label)

    chunk_reader = recorder.ChunkReader(
        sample_rate=sample_rate,
        threshold=threshold,
        silence_duration=silence_duration,
        **reader_kwargs,
    )

    keys = _KeyReader()
    track_num = 0
    shazam_id: _ShazamIdentifier | None = None
    shazam_id_sent = False
    track_paths: dict[int, Path] = {}

    # Now Playing state: poll when recording starts, cache result
    np_metadata: dict | None = None
    np_last_poll: float = 0.0
    NP_POLL_INTERVAL = 1.0      # re-poll every 1s during recording
    SHAZAM_DELAY = 15           # seconds before Shazam fallback

    # Rendering throttle
    last_render: float = 0.0
    RENDER_INTERVAL = 0.25       # 4 fps

    # TCC warning
    chunk_count = 0
    max_rms_seen = 0.0
    tcc_warned = False

    # ------------------------------------------------------------------
    # Process a completed track
    # ------------------------------------------------------------------
    def _reset_track_state() -> None:
        nonlocal shazam_id, shazam_id_sent, np_metadata
        shazam_id = None
        shazam_id_sent = False
        np_metadata = None
        state.early_id_result = None
        state.expected_duration = 0.0
        state.phase = "waiting"

    # Minimum recording length (seconds) to be considered a full track.
    # Anything shorter is discarded as a snippet/partial.
    MIN_TRACK_SECONDS = 60

    def process_track(audio: np.ndarray) -> None:
        nonlocal track_num

        recorded_secs = len(audio) / sample_rate

        # --- Resolve metadata ---
        metadata = None

        # 1. Now Playing (instant, free, has duration)
        if np_metadata and np_metadata.get("title"):
            metadata = dict(np_metadata)
            metadata["track"] = track_num

        # 2. Shazam early-ID
        if metadata is None and shazam_id and shazam_id.result:
            metadata = shazam_id.result
            metadata["track"] = track_num

        # 3. Manual mode
        if metadata is None and manual_mode:
            metadata = {
                "artist": artist,
                "album": album or "Unknown",
                "title": f"Track {track_num:02d}",
                "track": track_num,
                "cover_url": None,
            }

        # 4. Last resort: Shazam on the full track
        if metadata is None:
            state.phase = "identifying"
            state.error = None
            live.update(display.build_renderable(state))

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            recorder.write_wav(tmp_path, audio, sample_rate)
            try:
                metadata = identify.shazam_recognize(tmp_path)
            except Exception as e:
                state.error = f"Identification failed: {e}"
                metadata = {
                    "title": f"Track {track_num:02d}",
                    "artist": "Unknown",
                    "album": "Unknown",
                    "cover_url": None,
                }
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            metadata["track"] = track_num

        # --- Reject partial recordings ---
        expected_dur = metadata.get("duration", 0)
        if expected_dur and expected_dur > 0:
            # We know the expected length — reject if <80%
            if recorded_secs < expected_dur * 0.80:
                pct = int(recorded_secs / expected_dur * 100)
                state.error = (
                    f"Discarded partial: {metadata.get('artist', '?')} – "
                    f"{metadata.get('title', '?')} "
                    f"({_fmt_dur(recorded_secs)} / {_fmt_dur(expected_dur)}, {pct}%)"
                )
                state.skipped += 1
                _reset_track_state()
                return
        elif not manual_mode and recorded_secs < MIN_TRACK_SECONDS:
            # No duration info — use minimum length heuristic
            state.error = (
                f"Discarded short recording: {metadata.get('title', '?')} "
                f"({_fmt_dur(recorded_secs)} — under {MIN_TRACK_SECONDS}s minimum)"
            )
            state.skipped += 1
            _reset_track_state()
            return

        # --- Export to MP3 ---
        state.phase = "saving"
        live.update(display.build_renderable(state))

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        recorder.write_wav(tmp_path, audio, sample_rate)

        try:
            mp3_path = export.save_track(tmp_path, metadata, output)
            track_paths[track_num] = mp3_path
            state.completed.append(display.CompletedTrack(
                number=track_num,
                artist=metadata.get("artist", "Unknown"),
                title=metadata.get("title", "Unknown"),
                filename=mp3_path.name,
            ))
        except Exception as e:
            state.error = f"Export failed: {e}"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        _reset_track_state()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    live = display.make_live()

    try:
        keys.start()
        with live:
            live.update(display.build_renderable(state))

            for result in chunk_reader:
                chunk_count += 1
                max_rms_seen = max(max_rms_seen, result.rms)
                now = time.monotonic()

                # TCC warning
                if not tcc_warned and chunk_count > (3 * sample_rate // 1024) and max_rms_seen == 0.0:
                    state.error = (
                        "No audio detected. Grant 'Screen & System Audio Recording' "
                        "permission in System Settings > Privacy & Security, then restart."
                    )
                    tcc_warned = True

                # Handle keyboard
                key = keys.read_key()
                _handle_key(key, state, track_paths)

                # Raw RMS for dB VU meter (no artificial scaling)
                state.rms_level = result.rms

                if not result.track_complete:
                    if chunk_reader.recording:
                        if state.phase != "recording":
                            track_num += 1
                            state.phase = "recording"
                            state.current_track = track_num
                            state.record_start = now
                            state.early_id_result = None
                            state.expected_duration = 0.0
                            state.error = None
                            np_metadata = None
                            np_last_poll = 0.0

                        # Poll Now Playing
                        if has_nowplaying and not manual_mode and (now - np_last_poll) >= NP_POLL_INTERVAL:
                            np_last_poll = now
                            np_result = nowplaying.get_now_playing()
                            if np_result and np_result.get("title"):
                                np_metadata = np_result
                                dur = np_result.get("duration", 0)
                                state.expected_duration = dur if dur else 0.0
                                dur_str = f" ({_fmt_dur(dur)})" if dur else ""
                                state.early_id_result = (
                                    f"{np_result.get('artist', '?')} – {np_result.get('title', '?')}{dur_str}"
                                )

                        # Shazam fallback if Now Playing has nothing after 15s
                        if (
                            not manual_mode
                            and not shazam_id_sent
                            and np_metadata is None
                            and chunk_reader.recorded_seconds >= SHAZAM_DELAY
                        ):
                            partial = chunk_reader.current_audio()
                            if partial is not None:
                                shazam_id = _ShazamIdentifier(partial, sample_rate, state)
                                shazam_id.start()
                                shazam_id_sent = True
                    else:
                        if state.phase != "recording":
                            state.phase = "waiting"

                    # Throttled render
                    if now - last_render >= RENDER_INTERVAL:
                        live.update(display.build_renderable(state))
                        last_render = now

                else:
                    process_track(result.track_audio)
                    live.update(display.build_renderable(state))

    except KeyboardInterrupt:
        remaining = chunk_reader.flush()
        if remaining is not None and len(remaining) > sample_rate:
            track_num += 1
            state.current_track = track_num
            process_track(remaining)
    finally:
        keys.stop()

    total = len(state.completed)
    click.echo(f"\nDone. Saved {total} track{'s' if total != 1 else ''}.")
