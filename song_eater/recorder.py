"""Audio capture and silence-based track splitting."""

import numpy as np
import sounddevice as sd
import wave
from collections.abc import Generator


def find_device(name_substring: str) -> int:
    """Find an input device whose name contains the given substring."""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if name_substring.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    available = [
        f"  [{i}] {d['name']} (in={d['max_input_channels']}, out={d['max_output_channels']})"
        for i, d in enumerate(devices)
    ]
    raise RuntimeError(
        f"No input device matching '{name_substring}' found.\n"
        f"Available devices:\n" + "\n".join(available)
    )


def stream_tracks(
    device: int,
    sample_rate: int = 44100,
    channels: int = 2,
    threshold: float = 0.01,
    silence_duration: float = 3.0,
    chunk_frames: int = 1024,
) -> Generator[np.ndarray, None, None]:
    """Yield complete tracks by recording and splitting on silence.

    Blocks between yields. The caller should wrap in try/except
    KeyboardInterrupt to handle Ctrl+C and flush the partial buffer.
    """
    silence_chunks_needed = int(silence_duration * sample_rate / chunk_frames)

    with sd.InputStream(
        device=device,
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        blocksize=chunk_frames,
    ) as stream:
        chunks: list[np.ndarray] = []
        silent_count = 0
        has_audio = False

        while True:
            data, _ = stream.read(chunk_frames)
            rms = np.sqrt(np.mean(data**2))

            if rms >= threshold:
                has_audio = True
                silent_count = 0
                chunks.append(data.copy())
            else:
                if has_audio:
                    chunks.append(data.copy())
                    silent_count += 1

                    if silent_count >= silence_chunks_needed:
                        # Trim trailing silence
                        trim_count = min(silent_count, len(chunks))
                        audio = np.concatenate(chunks[:-trim_count] if trim_count else chunks)
                        yield audio
                        chunks.clear()
                        silent_count = 0
                        has_audio = False


def get_partial_track(chunks: list[np.ndarray]) -> np.ndarray | None:
    """Concatenate any remaining buffered chunks into a track."""
    if chunks:
        return np.concatenate(chunks)
    return None


def write_wav(path: str, audio: np.ndarray, sample_rate: int = 44100) -> None:
    """Write a float32 numpy array to a WAV file."""
    int16_audio = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    channels = int16_audio.shape[1] if int16_audio.ndim > 1 else 1
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_audio.tobytes())
