"""CLI entry point for song-eater with Rich TUI."""

from __future__ import annotations

import logging
import os
import select
import sys
import tempfile
import termios
import threading
import time
import traceback
import tty
from pathlib import Path

import click
import numpy as np

from song_eater import display, export, identify, itunes, nowplaying, recorder


def _fmt_dur(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def _disk_free_gb(path: Path) -> float:
    """Return free disk space in GB for the filesystem containing *path*."""
    st = os.statvfs(path)
    return (st.f_bavail * st.f_frsize) / (1024 ** 3)


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
# Keyboard logic
# ---------------------------------------------------------------------------

def _handle_key(key: str | None, state: display.TUIState) -> None:
    """Process a single keypress — arrow keys scroll the track list."""
    if key is None or not state.completed:
        return

    total = len(state.completed)
    vis = state.VISIBLE_ROWS
    max_offset = max(0, total - vis)

    if key == "up":
        state.scroll_pinned = False
        state.scroll_offset = max(0, state.scroll_offset - 1)
    elif key == "down":
        state.scroll_offset = min(max_offset, state.scroll_offset + 1)
        # Re-pin if scrolled back to the bottom
        if state.scroll_offset >= max_offset:
            state.scroll_pinned = True


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
  ↑ ↓          Scroll track list
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
    state = display.TUIState(source_name=source_label, output_dir=str(output))

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

    # Now Playing state
    np_metadata: dict | None = None       # winning metadata (resolved by vote)
    np_votes: list[dict] = []             # all polls collected during recording
    np_last_poll: float = 0.0
    np_current_title: str | None = None   # detect song changes (polled every 1s)
    NP_POLL_INTERVAL = 1.0       # title-change detection interval
    NP_VOTE_INTERVAL = 15.0      # collect a metadata vote every 15s
    np_last_vote: float = 0.0
    SHAZAM_DELAY = 15            # seconds before Shazam fallback
    itunes_lookup: itunes.ITunesLookup | None = None
    itunes_sent = False

    # False-split suppression: stash audio when silence triggers a split
    # but Now Playing says the same song is still playing
    stashed_audio: list[np.ndarray] = []

    # Rendering throttle
    last_render: float = 0.0
    RENDER_INTERVAL = 0.25       # 4 fps

    # Disk space monitoring
    DISK_CHECK_INTERVAL = 30.0   # check every 30s
    last_disk_check: float = 0.0
    MIN_DISK_GB = 0.5            # stop saving below this

    # TCC warning
    chunk_count = 0
    max_rms_seen = 0.0
    tcc_warned = False

    # ------------------------------------------------------------------
    # Process a completed track
    # ------------------------------------------------------------------
    def _reset_track_state() -> None:
        nonlocal shazam_id, shazam_id_sent, np_metadata, np_current_title, np_votes, np_last_vote
        nonlocal itunes_lookup, itunes_sent
        shazam_id = None
        shazam_id_sent = False
        np_metadata = None
        np_votes = []
        np_current_title = None
        np_last_vote = 0.0
        itunes_lookup = None
        itunes_sent = False
        stashed_audio.clear()
        state.early_id_result = None
        state.expected_duration = 0.0
        state.phase = "waiting"

    # Minimum recording length (seconds) to be considered a full track.
    # Anything shorter is discarded as a snippet/partial.
    MIN_TRACK_SECONDS = 60

    def _resolve_np_votes() -> dict | None:
        """Pick the winning Now Playing metadata by majority vote on title."""
        if not np_votes:
            return None
        # Count votes by title
        from collections import Counter
        title_counts = Counter(v.get("title") for v in np_votes)
        winning_title = title_counts.most_common(1)[0][0]
        # Return the most recent poll with the winning title (freshest artwork/duration)
        for v in reversed(np_votes):
            if v.get("title") == winning_title:
                return v
        return np_votes[-1]

    def process_track(audio: np.ndarray) -> None:
        nonlocal track_num, np_metadata

        recorded_secs = len(audio) / sample_rate

        # Resolve Now Playing from accumulated votes
        np_metadata = _resolve_np_votes()

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
            live.update(display.build_renderable(state, live.console))

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
        # Silently drop tiny snippets (< 20s)
        if recorded_secs < 20:
            _reset_track_state()
            return

        expected_dur = metadata.get("duration", 0)
        if expected_dur and expected_dur > 0:
            # We know the expected length — reject if <80%
            if recorded_secs < expected_dur * 0.80:
                pct = int(recorded_secs / expected_dur * 100)
                reason = f"{_fmt_dur(recorded_secs)}/{_fmt_dur(expected_dur)} ({pct}%)"
                state.completed.append(display.CompletedTrack(
                    number=track_num,
                    artist=metadata.get("artist", "?"),
                    title=metadata.get("title", "?"),
                    filename="",
                    discarded=True,
                    discard_reason=reason,
                ))
                state.scroll_pinned = True
                _reset_track_state()
                return
        elif not manual_mode and recorded_secs < MIN_TRACK_SECONDS:
            reason = f"{_fmt_dur(recorded_secs)} — under {MIN_TRACK_SECONDS}s"
            state.completed.append(display.CompletedTrack(
                number=track_num,
                artist=metadata.get("artist", "?"),
                title=metadata.get("title", "?"),
                filename="",
                discarded=True,
                discard_reason=reason,
            ))
            state.scroll_pinned = True
            _reset_track_state()
            return

        # --- Enrich with iTunes data (year, high-res artwork) ---
        # iTunes art is higher res than Now Playing; use it when available,
        # fall back to Now Playing art otherwise.
        if itunes_lookup and itunes_lookup.done and itunes_lookup.result:
            enrichment = itunes_lookup.result
            if enrichment.get("year") and "year" not in metadata:
                metadata["year"] = enrichment["year"]
            if enrichment.get("artwork_data"):
                metadata["artwork_data"] = enrichment["artwork_data"]
                metadata["artwork_mime"] = enrichment.get("artwork_mime", "image/jpeg")

        # --- Check disk space before saving ---
        free_gb = _disk_free_gb(output)
        state.disk_free_gb = free_gb
        if free_gb < MIN_DISK_GB:
            state.error = f"Disk full ({free_gb:.1f} GB free) — cannot save"
            state.completed.append(display.CompletedTrack(
                number=track_num,
                artist=metadata.get("artist", "?"),
                title=metadata.get("title", "?"),
                filename="",
                discarded=True,
                discard_reason=f"disk full ({free_gb:.1f} GB)",
            ))
            state.scroll_pinned = True
            _reset_track_state()
            return

        # --- Export to MP3 ---
        state.phase = "saving"
        live.update(display.build_renderable(state, live.console))

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            recorder.write_wav(tmp_path, audio, sample_rate)
            mp3_path = export.save_track(tmp_path, metadata, output)
            state.completed.append(display.CompletedTrack(
                number=track_num,
                artist=metadata.get("artist", "Unknown"),
                title=metadata.get("title", "Unknown"),
                filename=mp3_path.name,
            ))
            state.scroll_pinned = True
        except Exception as e:
            state.error = f"Export failed: {e}"
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        _reset_track_state()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    live = display.make_live()

    try:
        keys.start()
        with live:
            live.update(display.build_renderable(state, live.console))

            for result in chunk_reader:
                chunk_count += 1
                max_rms_seen = max(max_rms_seen, result.rms)
                now = time.monotonic()

                # TCC warning
                if not tcc_warned and chunk_count > (3 * sample_rate // 1024) and max_rms_seen == 0.0:
                    state.error = "No sound yet"
                    tcc_warned = True

                # Handle keyboard
                key = keys.read_key()
                _handle_key(key, state)

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

                        # Poll Now Playing (1s for title-change detection)
                        if has_nowplaying and not manual_mode and (now - np_last_poll) >= NP_POLL_INTERVAL:
                            np_last_poll = now
                            np_result = nowplaying.get_now_playing(source_app=process)
                            if np_result and np_result.get("title"):
                                new_title = np_result.get("title")

                                # Song changed — force-split the current track
                                if np_current_title and new_title != np_current_title:
                                    flushed = chunk_reader.flush()
                                    # Merge any stashed audio from suppressed splits
                                    parts = stashed_audio + ([flushed] if flushed is not None else [])
                                    stashed_audio.clear()
                                    if parts:
                                        merged = np.concatenate(parts)
                                        if len(merged) > sample_rate:
                                            process_track(merged)
                                            live.update(display.build_renderable(state, live.console))
                                    # Start fresh for the new song
                                    track_num += 1
                                    state.phase = "recording"
                                    state.current_track = track_num
                                    state.record_start = now
                                    state.early_id_result = None
                                    state.expected_duration = 0.0
                                    state.error = None
                                    shazam_id = None
                                    shazam_id_sent = False
                                    np_votes = []
                                    np_last_vote = now

                                np_current_title = new_title

                                # Collect a vote every NP_VOTE_INTERVAL (or on first poll)
                                if not np_votes or (now - np_last_vote) >= NP_VOTE_INTERVAL:
                                    np_votes.append(np_result)
                                    np_last_vote = now

                                # Fire iTunes lookup on first identification
                                if not itunes_sent:
                                    itunes_lookup = itunes.ITunesLookup(
                                        np_result.get("artist", ""),
                                        np_result.get("title", ""),
                                    )
                                    itunes_lookup.start()
                                    itunes_sent = True

                                # Display uses most recent poll for responsiveness
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

                    # Duration overrun — song should be done, force-split
                    # Runs outside chunk_reader.recording gate so it fires even
                    # when audio goes silent after a false-split stash.
                    if (
                        state.expected_duration > 0
                        and state.phase == "recording"
                        and (now - state.record_start) > state.expected_duration + 5.0
                    ):
                        flushed = chunk_reader.flush()
                        parts = stashed_audio + ([flushed] if flushed is not None else [])
                        stashed_audio.clear()
                        if parts:
                            merged = np.concatenate(parts)
                            if len(merged) > sample_rate:
                                process_track(merged)
                                live.update(display.build_renderable(state, live.console))
                        # Reset for next track
                        track_num += 1
                        state.phase = "waiting"
                        state.current_track = track_num
                        state.record_start = now
                        state.early_id_result = None
                        state.expected_duration = 0.0
                        state.error = None
                        shazam_id = None
                        shazam_id_sent = False
                        np_votes = []
                        np_last_vote = now
                        np_current_title = None
                        itunes_lookup = None
                        itunes_sent = False

                    # Periodic disk space check
                    if now - last_disk_check >= DISK_CHECK_INTERVAL:
                        state.disk_free_gb = _disk_free_gb(output)
                        last_disk_check = now

                    # Throttled render
                    if now - last_render >= RENDER_INTERVAL:
                        live.update(display.build_renderable(state, live.console))
                        last_render = now

                else:
                    # Silence triggered a split — but is the same song still playing?
                    np_still_playing = False
                    if has_nowplaying and not manual_mode and np_current_title:
                        np_check = nowplaying.get_now_playing(source_app=process)
                        if np_check and np_check.get("title") == np_current_title:
                            np_still_playing = True

                    if np_still_playing:
                        # False split — stash the audio fragment and keep going
                        stashed_audio.append(result.track_audio)
                    else:
                        # Real split — merge any stashed fragments and process
                        if stashed_audio:
                            stashed_audio.append(result.track_audio)
                            merged = np.concatenate(stashed_audio)
                            stashed_audio.clear()
                            process_track(merged)
                        else:
                            process_track(result.track_audio)
                        live.update(display.build_renderable(state, live.console))

    except KeyboardInterrupt:
        remaining = chunk_reader.flush()
        if remaining is not None and len(remaining) > sample_rate:
            track_num += 1
            state.current_track = track_num
            process_track(remaining)
    except Exception:
        # Log the full traceback so silent crashes leave a trail
        crash_log = output / "song-eater-crash.log"
        tb = traceback.format_exc()
        try:
            with open(crash_log, "a") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(tb)
        except OSError:
            pass  # disk truly full — nothing we can do
        # Show the user what happened
        click.echo(f"\n\nCrashed unexpectedly:\n{tb}", err=True)
        click.echo(f"Full traceback written to {crash_log}", err=True)
    finally:
        keys.stop()

    total = len(state.completed)
    click.echo(f"\nDone. Saved {total} track{'s' if total != 1 else ''}.")
