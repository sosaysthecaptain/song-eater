"""Song identification: macOS Now Playing (primary) + Shazam (fallback)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from shazamio import Shazam

from song_eater import nowplaying


# ---------------------------------------------------------------------------
# Now Playing (primary — instant, free, 100% accurate)
# ---------------------------------------------------------------------------

def identify_from_now_playing() -> dict | None:
    """Read current track metadata from macOS Now Playing.

    Returns a metadata dict or None if nothing is playing / unavailable.
    """
    info = nowplaying.get_now_playing()
    if info is None:
        return None
    return {
        "title": info["title"],
        "artist": info["artist"],
        "album": info["album"],
        "cover_url": None,
    }


# ---------------------------------------------------------------------------
# Shazam (fallback)
# ---------------------------------------------------------------------------

async def _shazam_recognize(wav_path: str) -> dict:
    """Single Shazam recognition attempt."""
    shazam = Shazam()
    result = await shazam.recognize(wav_path)

    if "track" not in result:
        return {
            "title": "Unknown",
            "artist": "Unknown",
            "album": "Unknown",
            "cover_url": None,
        }

    track = result["track"]
    return {
        "title": track.get("title", "Unknown"),
        "artist": track.get("subtitle", "Unknown"),
        "album": _extract_album(track),
        "cover_url": _extract_cover(track),
    }


def shazam_recognize(wav_path: str) -> dict:
    """Identify a song from a WAV file via Shazam. Returns metadata dict."""
    return asyncio.run(_shazam_recognize(wav_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_album(track: dict) -> str:
    for section in track.get("sections", []):
        if section.get("type") == "SONG":
            for item in section.get("metadata", []):
                if item.get("title", "").lower() == "album":
                    return item.get("text", "Unknown")
    return "Unknown"


def _extract_cover(track: dict) -> str | None:
    images = track.get("images", {})
    return images.get("coverart") or images.get("coverarthq")
