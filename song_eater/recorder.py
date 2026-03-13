"""Audio capture and silence-based track splitting."""

import subprocess
import sys
import wave
from collections.abc import Generator
from pathlib import Path

import numpy as np
import sounddevice as sd


AUDIO_TAP_PATH = Path(__file__).parent / "audio_tap"


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


def _read_chunks_from_process_tap(
    process_name: str,
    sample_rate: int,
    channels: int,
    chunk_frames: int,
) -> Generator[np.ndarray, None, None]:
    """Read float32 PCM chunks from the audio_tap Swift helper via stdout pipe.

    The tap outputs raw interleaved float32 stereo at its native sample rate
    (usually 48000 Hz).  We read it as float32 and reshape to (frames, channels).
    """
    if not AUDIO_TAP_PATH.exists():
        raise RuntimeError(
            f"audio_tap binary not found at {AUDIO_TAP_PATH}.\n"
            f"Build it with: make build"
        )

    # Use system-wide tap for reliability (per-process tap IOProc doesn't fire).
    # The --system flag captures all system audio.
    proc = subprocess.Popen(
        [str(AUDIO_TAP_PATH), "--system", "all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Parse stderr for SAMPLE_RATE/CHANNELS and forward the rest
    import threading
    tap_sample_rate = [0]
    tap_channels = [0]
    ready_event = threading.Event()

    def _drain_stderr():
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line.startswith("[audio_tap] SAMPLE_RATE="):
                tap_sample_rate[0] = int(line.split("=", 1)[1])
            elif line.startswith("[audio_tap] CHANNELS="):
                tap_channels[0] = int(line.split("=", 1)[1])
                ready_event.set()
            # Swallow audio_tap debug output — don't pollute the TUI
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Wait for the tap to report its format (up to 5 seconds)
    if not ready_event.wait(timeout=5):
        proc.terminate()
        proc.wait()
        raise RuntimeError("audio_tap did not report sample rate in time")

    tap_sr = tap_sample_rate[0] or sample_rate
    tap_ch = tap_channels[0] or channels

    # float32 = 4 bytes per sample
    bytes_per_chunk = chunk_frames * tap_ch * 4

    try:
        while True:
            data = proc.stdout.read(bytes_per_chunk)
            if not data:
                break
            float_data = np.frombuffer(data, dtype=np.float32)
            expected = chunk_frames * tap_ch
            if len(float_data) < expected:
                float_data = np.pad(float_data, (0, expected - len(float_data)))
            float_data = float_data.reshape(-1, tap_ch)
            # Trim or duplicate channels to match requested channel count
            if tap_ch > channels:
                float_data = float_data[:, :channels]
            elif tap_ch < channels:
                float_data = np.column_stack([float_data] * (channels // tap_ch + 1))[:, :channels]
            yield float_data
    finally:
        proc.terminate()
        proc.wait()


def _read_chunks_from_device(
    device: int,
    sample_rate: int,
    channels: int,
    chunk_frames: int,
) -> Generator[np.ndarray, None, None]:
    """Read audio chunks from a sounddevice input."""
    # Use the device's actual channel count if it has fewer than requested
    dev_info = sd.query_devices(device)
    dev_channels = min(channels, dev_info["max_input_channels"])
    with sd.InputStream(
        device=device,
        samplerate=sample_rate,
        channels=dev_channels,
        dtype="float32",
        blocksize=chunk_frames,
    ) as stream:
        while True:
            data, _ = stream.read(chunk_frames)
            # If mono device but stereo requested, duplicate to stereo
            if dev_channels == 1 and channels == 2:
                data = np.column_stack([data, data])
            yield data.copy()


def stream_tracks(
    sample_rate: int = 44100,
    channels: int = 2,
    threshold: float = 0.01,
    silence_duration: float = 3.0,
    chunk_frames: int = 1024,
    device: int | None = None,
    process_name: str | None = None,
) -> Generator[np.ndarray, None, None]:
    """Yield complete tracks by recording and splitting on silence.

    Provide either `device` (sounddevice index) or `process_name` (for CoreAudio tap).
    """
    silence_chunks_needed = int(silence_duration * sample_rate / chunk_frames)

    if process_name:
        chunk_source = _read_chunks_from_process_tap(process_name, sample_rate, channels, chunk_frames)
    elif device is not None:
        chunk_source = _read_chunks_from_device(device, sample_rate, channels, chunk_frames)
    else:
        raise ValueError("Must provide either device or process_name")

    chunks: list[np.ndarray] = []
    silent_count = 0
    has_audio = False

    for data in chunk_source:
        rms = np.sqrt(np.mean(data**2))

        if rms >= threshold:
            has_audio = True
            silent_count = 0
            chunks.append(data)
        else:
            if has_audio:
                chunks.append(data)
                silent_count += 1

                if silent_count >= silence_chunks_needed:
                    # Trim trailing silence
                    trim_count = min(silent_count, len(chunks))
                    audio = np.concatenate(chunks[:-trim_count] if trim_count else chunks)
                    yield audio
                    chunks.clear()
                    silent_count = 0
                    has_audio = False


# ---------------------------------------------------------------------------
# ChunkReader -- low-level interface for the TUI to drive the record loop
# ---------------------------------------------------------------------------

class ChunkResult:
    """Result from reading one audio chunk."""

    __slots__ = ("chunk", "rms", "is_silent", "track_complete", "track_audio")

    def __init__(
        self,
        chunk: np.ndarray,
        rms: float,
        is_silent: bool,
        track_complete: bool,
        track_audio: np.ndarray | None,
    ):
        self.chunk = chunk
        self.rms = rms
        self.is_silent = is_silent
        self.track_complete = track_complete
        self.track_audio = track_audio


class ChunkReader:
    """Chunk-level recording interface that exposes per-chunk RMS and silence state.

    Usage::

        reader = ChunkReader(sample_rate=44100, threshold=0.01,
                             silence_duration=3.0, process_name="Chrome")
        for result in reader:
            # result.rms      -- current RMS level
            # result.is_silent -- whether this chunk was below threshold
            # result.track_complete -- True when a full track boundary was found
            # result.track_audio    -- the complete track audio (only when track_complete)
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        channels: int = 2,
        threshold: float = 0.01,
        silence_duration: float = 3.0,
        chunk_frames: int = 1024,
        device: int | None = None,
        process_name: str | None = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.threshold = threshold
        self.chunk_frames = chunk_frames
        self.silence_chunks_needed = int(silence_duration * sample_rate / chunk_frames)

        if process_name:
            self._source = _read_chunks_from_process_tap(process_name, sample_rate, channels, chunk_frames)
        elif device is not None:
            self._source = _read_chunks_from_device(device, sample_rate, channels, chunk_frames)
        else:
            raise ValueError("Must provide either device or process_name")

        self._chunks: list[np.ndarray] = []
        self._silent_count = 0
        self._has_audio = False

    @property
    def recording(self) -> bool:
        """True when we have captured at least one non-silent chunk."""
        return self._has_audio

    @property
    def recorded_frames(self) -> int:
        """Number of audio frames accumulated so far in the current track."""
        return len(self._chunks) * self.chunk_frames

    @property
    def recorded_seconds(self) -> float:
        return self.recorded_frames / self.sample_rate

    def current_audio(self) -> np.ndarray | None:
        """Return audio captured so far (for early identification). May be expensive for long tracks."""
        if not self._chunks:
            return None
        return np.concatenate(self._chunks)

    def __iter__(self):
        return self

    def __next__(self) -> ChunkResult:
        try:
            data = next(self._source)
        except StopIteration:
            # Flush remaining audio as a final track
            if self._has_audio and self._chunks:
                audio = np.concatenate(self._chunks)
                self._chunks.clear()
                self._has_audio = False
                raise StopIteration  # caller should check for leftover via flush()
            raise

        rms = float(np.sqrt(np.mean(data ** 2)))
        is_silent = rms < self.threshold
        track_complete = False
        track_audio = None

        if not is_silent:
            self._has_audio = True
            self._silent_count = 0
            self._chunks.append(data)
        else:
            if self._has_audio:
                self._chunks.append(data)
                self._silent_count += 1

                if self._silent_count >= self.silence_chunks_needed:
                    trim_count = min(self._silent_count, len(self._chunks))
                    track_audio = np.concatenate(
                        self._chunks[:-trim_count] if trim_count else self._chunks
                    )
                    track_complete = True
                    self._chunks.clear()
                    self._silent_count = 0
                    self._has_audio = False

        return ChunkResult(
            chunk=data,
            rms=rms,
            is_silent=is_silent,
            track_complete=track_complete,
            track_audio=track_audio,
        )

    def flush(self) -> np.ndarray | None:
        """Return any remaining audio when shutting down."""
        if self._has_audio and self._chunks:
            audio = np.concatenate(self._chunks)
            self._chunks.clear()
            self._has_audio = False
            return audio
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
