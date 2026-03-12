"""Song identification via Shazam fingerprinting."""

from shazamio import Shazam


async def _recognize(wav_path: str) -> dict:
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


def recognize(wav_path: str) -> dict:
    """Identify a song from a WAV file. Returns metadata dict."""
    import asyncio

    return asyncio.run(_recognize(wav_path))


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
