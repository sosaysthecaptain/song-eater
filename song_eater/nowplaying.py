"""Poll macOS Now Playing metadata via media-control."""

from __future__ import annotations

import base64
import json
import subprocess


def get_now_playing() -> dict | None:
    """Return current Now Playing metadata, or None if nothing is playing.

    Returns dict with keys: title, artist, album, duration (seconds, 0 if unknown).
    Requires ``media-control`` (``brew tap ungive/media-control && brew install media-control``).
    """
    try:
        result = subprocess.run(
            ["media-control", "get"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)

        title = data.get("title")
        if not title:
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
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, Exception):
        return None


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
