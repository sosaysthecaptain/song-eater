"""Poll macOS Now Playing metadata via media-control."""

from __future__ import annotations

import base64
import json
import subprocess
import threading


def _poll() -> dict | None:
    """Single raw poll of media-control."""
    try:
        result = subprocess.run(
            ["media-control", "get"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        return None


def get_now_playing(source_app: str | None = None) -> dict | None:
    """Return current Now Playing metadata, or None if nothing is playing.

    Returns dict with keys: title, artist, album, duration (seconds, 0 if unknown).
    Requires ``media-control`` (``brew tap ungive/media-control && brew install media-control``).

    If *source_app* is given, only returns metadata from that app
    (matched case-insensitively against bundleIdentifier or applicationName).
    Rejects stale metadata where playback is paused/stopped.
    """
    data = _poll()
    if not data:
        return None

    # Reject if not actively playing (stale metadata from yesterday's Twitter video)
    if not data.get("playing", False):
        return None

    title = data.get("title")
    if not title:
        return None

    # Reject if metadata is from a different app than we're capturing
    if source_app:
        bundle = (data.get("bundleIdentifier") or "").lower()
        app_lower = source_app.lower()
        if app_lower not in bundle and bundle not in app_lower:
            return None

    artist = data.get("artist", "Unknown") or "Unknown"
    album = data.get("album", "Unknown") or "Unknown"

    try:
        duration = float(data.get("duration", 0))
    except (ValueError, TypeError):
        duration = 0.0

    artwork_data = None
    artwork_mime = data.get("artworkMimeType", "image/jpeg")
    raw_art = data.get("artworkData")
    if raw_art:
        try:
            artwork_data = base64.b64decode(raw_art)
        except Exception:
            artwork_data = None

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "artwork_data": artwork_data,
        "artwork_mime": artwork_mime,
    }


class NowPlayingPoller:
    """Poll Now Playing on a background thread and cache the latest result.

    The capture loop must never block on ``media-control`` — a synchronous poll
    stalls the loop, backs up the audio pipe, and drops audio (see
    ``recorder._threaded_chunks``). This runs the subprocess off the hot path;
    the loop reads the cached value via :meth:`latest`, which never blocks.
    """

    def __init__(self, source_app: str | None = None, interval: float = 0.5):
        self._source_app = source_app
        self._interval = interval
        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "NowPlayingPoller":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            result = get_now_playing(source_app=self._source_app)
            with self._lock:
                self._latest = result
            self._stop.wait(self._interval)

    def latest(self) -> dict | None:
        """Return the most recent poll result (non-blocking)."""
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()


def is_available() -> bool:
    """Check if media-control is installed and functional."""
    try:
        result = subprocess.run(
            ["media-control", "test"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, Exception):
        return False
