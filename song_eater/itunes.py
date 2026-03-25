"""Enrich metadata via the iTunes Search API (year, high-res artwork)."""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request


class ITunesLookup:
    """Fire-and-forget iTunes Search query. Results available via .result property."""

    def __init__(self, artist: str, title: str, album: str = "",
                 collection_id: int | None = None):
        self._artist = artist
        self._title = title
        self._album = album
        self._collection_id = collection_id
        self._result: dict | None = None
        self._done = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self._result = search(
                self._artist, self._title, self._album,
                collection_id=self._collection_id,
            )
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


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation/whitespace for fuzzy comparison."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _title_matches(a: str, b: str) -> bool:
    """Check if two track titles refer to the same piece."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _fetch_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "song-eater/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_artwork(url: str) -> bytes | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None


def _build_result(hit: dict, collection_id: int | None = None) -> dict:
    """Build a result dict from an iTunes track record."""
    year = None
    release = hit.get("releaseDate", "")
    if release and len(release) >= 4:
        year = release[:4]

    artwork_url = hit.get("artworkUrl100", "")
    if artwork_url:
        artwork_url = artwork_url.replace("100x100bb", "1200x1200bb")

    return {
        "year": year,
        "album": hit.get("collectionName", ""),
        "album_artist": hit.get("artistName", ""),
        "track_number": hit.get("trackNumber"),
        "disc_number": hit.get("discNumber"),
        "collection_id": collection_id or hit.get("collectionId"),
        "artwork_url": artwork_url,
        "artwork_data": _fetch_artwork(artwork_url),
        "artwork_mime": "image/jpeg",
        "album_match": True,
    }


def _find_track_in_collection(collection_id: int, title: str) -> dict | None:
    """Look up all tracks in a collection and find one matching *title*."""
    lookup_url = (
        f"https://itunes.apple.com/lookup"
        f"?id={collection_id}&entity=song"
    )
    lookup_data = _fetch_json(lookup_url)
    if not lookup_data:
        return None

    for item in lookup_data.get("results", []):
        if item.get("wrapperType") != "track":
            continue
        if _title_matches(title, item.get("trackName", "")):
            return _build_result(item, collection_id)
    return None


def search(artist: str, title: str, album: str = "",
           collection_id: int | None = None) -> dict | None:
    """Search iTunes, album-first strategy.

    If *collection_id* is provided, skips the album search and goes straight
    to looking up the track in that collection (cache hit from a previous track).

    Otherwise:
    1. Search for the album, find the right collection.
    2. Look up all tracks in that collection.
    3. Match our track by title.

    Falls back to a direct song search if album lookup fails,
    but marks the result so the caller knows it's not album-verified.
    """

    # --- Fast path: cached collection from a previous track ---
    if collection_id:
        result = _find_track_in_collection(collection_id, title)
        if result:
            return result

    # --- Strategy 1: Album-first lookup ---
    if album:
        # Use first artist name only (full ensemble strings confuse search)
        short_artist = artist.split(",")[0].strip()
        album_query = f"{short_artist} {album}"
        params = urllib.parse.urlencode({
            "term": album_query,
            "media": "music",
            "entity": "album",
            "limit": "5",
        })
        data = _fetch_json(f"https://itunes.apple.com/search?{params}")
        if data:
            for album_hit in data.get("results", []):
                cid = album_hit.get("collectionId")
                if not cid:
                    continue
                result = _find_track_in_collection(cid, title)
                if result:
                    return result

    # --- Strategy 2: Direct song search (fallback) ---
    query = f"{artist} {title}".strip()
    params = urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "limit": "1",
    })
    data = _fetch_json(f"https://itunes.apple.com/search?{params}")
    if not data:
        return None

    results = data.get("results", [])
    if not results:
        return None

    hit = results[0]
    result = _build_result(hit)
    result["album_match"] = False  # not verified — caller should be cautious
    return result
