"""CLI entry point for song-eater."""

import tempfile
from pathlib import Path

import click
import numpy as np
import sounddevice as sd

from song_eater import display, export, identify, recorder


@click.command()
@click.option("--device", "-d", default="BlackHole", help="Audio input device name (substring match)")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Output directory (default: current dir)")
@click.option("--artist", "-a", default=None, help="Artist name (enables manual mode)")
@click.option("--album", "-A", default=None, help="Album name (manual mode)")
@click.option("--threshold", "-t", type=float, default=0.01, help="RMS silence threshold")
@click.option("--silence-duration", "-s", type=float, default=3.0, help="Seconds of silence to split tracks")
@click.option("--sample-rate", type=int, default=44100, help="Sample rate in Hz")
def main(device, output, artist, album, threshold, silence_duration, sample_rate):
    """Record system audio, split tracks, identify, tag, and save as MP3."""
    if output is None:
        output = Path.cwd()
    output.mkdir(parents=True, exist_ok=True)

    manual_mode = artist is not None

    try:
        device_index = recorder.find_device(device)
    except RuntimeError as e:
        display.show_error(str(e))
        raise SystemExit(1)

    device_name = sd.query_devices(device_index)["name"]
    display.show_listening(device_name)

    track_num = 0
    # Keep a reference to current chunks for flushing on Ctrl+C
    current_chunks: list[np.ndarray] = []

    def process_track(audio: np.ndarray) -> None:
        nonlocal track_num
        track_num += 1
        display.show_recording(track_num)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        recorder.write_wav(tmp_path, audio, sample_rate)

        if manual_mode:
            metadata = {
                "artist": artist,
                "album": album or "Unknown",
                "title": f"Track {track_num:02d}",
                "track": track_num,
                "cover_url": None,
            }
        else:
            display.show_identifying()
            try:
                metadata = identify.recognize(tmp_path)
            except Exception as e:
                display.show_error(f"Identification failed: {e}")
                metadata = {
                    "title": f"Track {track_num:02d}",
                    "artist": "Unknown",
                    "album": "Unknown",
                    "cover_url": None,
                }
            metadata["track"] = track_num

        try:
            mp3_path = export.save_track(tmp_path, metadata, output)
            display.show_saved(track_num, mp3_path, metadata)
        except Exception as e:
            display.show_error(f"Export failed: {e}")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    try:
        for audio in recorder.stream_tracks(
            device=device_index,
            sample_rate=sample_rate,
            threshold=threshold,
            silence_duration=silence_duration,
        ):
            process_track(audio)
    except KeyboardInterrupt:
        display.console.print("\n[dim]Stopping...[/]")

    display.show_done(track_num)
