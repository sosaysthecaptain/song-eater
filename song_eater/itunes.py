"""Enrich metadata via the iTunes Search API (year, high-res artwork)."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request


class ITunesLookup:
    """Fire-and-forget iTunes Search query. Results available via .result property."""

    def __init__(self, artist: str, title: str):
        self._artist = artist
        self._title = title
        self._result: dict | None = None
        self._done = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self._result = search(self._artist, self._title)
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


def search(artist: str, title: str) -> dict | None:
    """Query iTunes Search API and return enrichment data.

    Returns dict with keys: year, artwork_url, artwork_data, artwork_mime.
    Returns None if no match found.
    """
    query = f"{artist} {title}"
    params = urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "limit": "1",
    })
    url = f"https://itunes.apple.com/search?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "song-eater/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    hit = results[0]

    # Extract year from releaseDate (e.g. "1998-01-01T12:00:00Z")
    year = None
    release = hit.get("releaseDate", "")
    if release and len(release) >= 4:
        year = release[:4]

    # Get high-res artwork (swap 100x100 to 1200x1200)
    artwork_url = hit.get("artworkUrl100", "")
    if artwork_url:
        artwork_url = artwork_url.replace("100x100bb", "1200x1200bb")

    # Fetch the artwork
    artwork_data = None
    if artwork_url:
        try:
            with urllib.request.urlopen(artwork_url, timeout=10) as resp:
                artwork_data = resp.read()
        except Exception:
            pass

    return {
        "year": year,
        "album": hit.get("collectionName", ""),
        "artwork_url": artwork_url,
        "artwork_data": artwork_data,
        "artwork_mime": "image/jpeg",
    }
