"""CLI entry point for song-eater with Rich TUI."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import click
import numpy as np
import sounddevice as sd

from song_eater import display, export, identify, recorder


# ---------------------------------------------------------------------------
# Early identification helper (runs in a background thread)
# ---------------------------------------------------------------------------

class _EarlyIdentifier:
    """Fire-and-forget Shazam identification on a partial recording."""

    def __init__(self, audio: np.ndarray, sample_rate: int, state: display.TUIState):
        self._audio = audio
        self._sr = sample_rate
        self._state = state
        self._result: dict | None = None
        self._thread: threading.Thread | None = None
        self._done = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            recorder.write_wav(tmp_path, self._audio, self._sr)
            result = identify.recognize(tmp_path)
            Path(tmp_path).unlink(missing_ok=True)
            if result.get("title", "Unknown") != "Unknown":
                self._result = result
                self._state.early_id_result = (
                    f"{result.get('artist', '?')} - {result.get('title', '?')}"
                )
        except Exception:
            pass  # silently ignore early-ID failures
        finally:
            self._done = True

    @property
    def done(self) -> bool:
        return self._done

    @property
    def result(self) -> dict | None:
        return self._result


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--process", "-p", default=None, help="Capture audio from a specific app (e.g. 'Chrome', 'Spotify')")
@click.option("--device", "-d", default=None, help="Audio input device name (substring match, e.g. 'BlackHole')")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Output directory (default: current dir)")
@click.option("--artist", "-a", default=None, help="Artist name (enables manual mode)")
@click.option("--album", "-A", default=None, help="Album name (manual mode)")
@click.option("--threshold", "-t", type=float, default=0.01, help="RMS silence threshold")
@click.option("--silence-duration", "-s", type=float, default=3.0, help="Seconds of silence to split tracks")
@click.option("--sample-rate", type=int, default=48000, help="Sample rate in Hz")
def main(process, device, output, artist, album, threshold, silence_duration, sample_rate):
    """Record system audio, split tracks, identify, tag, and save as MP3."""
    if output is None:
        output = Path.cwd()
    output.mkdir(parents=True, exist_ok=True)

    manual_mode = artist is not None

    # ------------------------------------------------------------------
    # Resolve capture source
    # ------------------------------------------------------------------
    if process:
        source_label = process
        reader_kwargs = dict(process_name=process)
    elif device:
        try:
            device_index = recorder.find_device(device)
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)
        source_label = sd.query_devices(device_index)["name"]
        reader_kwargs = dict(device=device_index)
    else:
        try:
            device_index = recorder.find_device("BlackHole")
            source_label = sd.query_devices(device_index)["name"]
            reader_kwargs = dict(device=device_index)
        except RuntimeError:
            click.echo(
                "Error: No audio source specified.\n\n"
                "  Capture from a specific app (recommended):\n"
                "    song-eater --process Chrome\n"
                "    song-eater --process Spotify\n\n"
                "  Or use an audio input device:\n"
                "    song-eater --device BlackHole\n",
                err=True,
            )
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # Set up TUI state and chunk reader
    # ------------------------------------------------------------------
    state = display.TUIState(source_name=source_label)

    chunk_reader = recorder.ChunkReader(
        sample_rate=sample_rate,
        threshold=threshold,
        silence_duration=silence_duration,
        **reader_kwargs,
    )

    track_num = 0
    early_id: _EarlyIdentifier | None = None
    early_id_sent = False  # True once we fired early ID for the current track

    EARLY_ID_SECONDS = 15  # attempt Shazam after this many seconds

    # ------------------------------------------------------------------
    # Process a completed track (blocking -- runs in the main thread)
    # ------------------------------------------------------------------
    def process_track(audio: np.ndarray) -> None:
        nonlocal track_num, early_id, early_id_sent

        # Use early-ID result if available; otherwise identify now
        metadata = None
        if early_id and early_id.result:
            metadata = early_id.result
            metadata["track"] = track_num

        state.phase = "identifying"
        state.error = None
        live.update(display.build_renderable(state))

        if metadata is None:
            if manual_mode:
                metadata = {
                    "artist": artist,
                    "album": album or "Unknown",
                    "title": f"Track {track_num:02d}",
                    "track": track_num,
                    "cover_url": None,
                }
            else:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = tmp.name
                recorder.write_wav(tmp_path, audio, sample_rate)
                try:
                    metadata = identify.recognize(tmp_path)
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

        state.phase = "saving"
        live.update(display.build_renderable(state))

        # Write WAV for export
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        recorder.write_wav(tmp_path, audio, sample_rate)

        try:
            mp3_path = export.save_track(tmp_path, metadata, output)
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

        # Reset for next track
        early_id = None
        early_id_sent = False
        state.early_id_result = None
        state.phase = "waiting"

    # ------------------------------------------------------------------
    # Main loop with Live TUI
    # ------------------------------------------------------------------
    live = display.make_live()

    chunk_count = 0
    max_rms_seen = 0.0
    tcc_warned = False

    try:
        with live:
            live.update(display.build_renderable(state))

            for result in chunk_reader:
                chunk_count += 1
                max_rms_seen = max(max_rms_seen, result.rms)

                # Warn about TCC if we've seen ~3 seconds of pure silence
                if not tcc_warned and chunk_count > (3 * sample_rate // 1024) and max_rms_seen == 0.0:
                    state.error = (
                        "No audio detected. Grant 'Screen & System Audio Recording' "
                        "permission in System Settings > Privacy & Security, then restart."
                    )
                    tcc_warned = True

                # Update level meter (apply some smoothing for display)
                state.rms_level = min(result.rms * 5.0, 1.0)  # scale up for visibility

                # Phase transitions based on chunk reader state
                if not result.track_complete:
                    if chunk_reader.recording:
                        if state.phase != "recording":
                            track_num += 1
                            state.phase = "recording"
                            state.current_track = track_num
                            state.record_start = time.monotonic()
                            state.early_id_result = None
                            state.error = None

                        # Fire early identification after ~15 seconds
                        if (
                            not manual_mode
                            and not early_id_sent
                            and chunk_reader.recorded_seconds >= EARLY_ID_SECONDS
                        ):
                            partial_audio = chunk_reader.current_audio()
                            if partial_audio is not None:
                                early_id = _EarlyIdentifier(partial_audio, sample_rate, state)
                                early_id.start()
                                early_id_sent = True
                    else:
                        if state.phase == "recording":
                            # Shouldn't normally happen (track_complete handles this)
                            pass
                        else:
                            state.phase = "waiting"

                    # Refresh TUI (~every chunk, limited by Live's refresh rate)
                    live.update(display.build_renderable(state))

                else:
                    # A track boundary was detected
                    process_track(result.track_audio)
                    live.update(display.build_renderable(state))

    except KeyboardInterrupt:
        # Flush any remaining audio
        remaining = chunk_reader.flush()
        if remaining is not None and len(remaining) > sample_rate:  # at least 1s
            track_num += 1
            state.current_track = track_num
            process_track(remaining)

    # Final summary
    total = len(state.completed)
    click.echo(f"\nDone. Saved {total} track{'s' if total != 1 else ''}.")
