"""Poll macOS Now Playing metadata via media-control."""

from __future__ import annotations

import base64
import json
import subprocess


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
