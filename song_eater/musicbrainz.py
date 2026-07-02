"""MusicBrainz + Cover Art Archive lookups for the retag pass.

Keyless. MusicBrainz asks for a descriptive User-Agent and no more than ~1
request/second, so every call goes through a shared throttle and an in-memory
cache keyed by URL.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

_UA = "song-eater/1.0 (https://github.com/sosaysthecaptain/song-eater)"
_MB = "https://musicbrainz.org/ws/2"
_CAA = "https://coverartarchive.org"

_MIN_INTERVAL = 1.1  # seconds between MusicBrainz requests
_last_request = [0.0]
_throttle_lock = threading.Lock()
_cache: dict[str, object] = {}


def _mb_get(url: str) -> dict | None:
    """GET a MusicBrainz JSON endpoint, throttled, cached, with retries.

    MusicBrainz returns 503 under load; a transient failure here would silently
    downgrade an album match (e.g. picking a compilation because the original's
    tracklist didn't load), so we retry and never cache a failure.
    """
    if url in _cache:
        return _cache[url]  # type: ignore[return-value]
    for attempt in range(3):
        with _throttle_lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_request[0])
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            _cache[url] = data
            return data
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None  # don't cache transient failures


# --- Type/date scoring inputs -------------------------------------------------

def release_group_kind(rg: dict) -> tuple[str, list[str]]:
    """Return (primary_type, secondary_types) for a release-group record."""
    primary = rg.get("primary-type") or ""
    secondary = rg.get("secondary-types") or []
    return primary, list(secondary)


def first_year(rg: dict) -> str:
    d = rg.get("first-release-date") or ""
    return d[:4] if len(d) >= 4 else ""


# --- Searches -----------------------------------------------------------------

def search_release_groups(artist: str, album: str, limit: int = 8) -> list[dict]:
    """Search release-groups matching an artist + album title."""
    query = f'releasegroup:"{album}" AND artist:"{artist}"'
    url = f"{_MB}/release-group?query={urllib.parse.quote(query)}&fmt=json&limit={limit}"
    data = _mb_get(url)
    return data.get("release-groups", []) if data else []


def search_recordings(artist: str, title: str, limit: int = 10) -> list[dict]:
    """Search recordings (individual songs), including their release-groups."""
    query = f'recording:"{title}" AND artist:"{artist}"'
    url = f"{_MB}/recording?query={urllib.parse.quote(query)}&fmt=json&limit={limit}"
    data = _mb_get(url)
    return data.get("recordings", []) if data else []


def recording_release_groups(rec_id: str) -> list[dict]:
    """All release-groups a recording appears on (with type + release date)."""
    data = _mb_get(f"{_MB}/recording/{rec_id}?inc=release-groups&fmt=json")
    return data.get("release-groups", []) if data else []


def release_group_tracklist(rg_id: str) -> tuple[list[tuple[int, int, str]], str]:
    """Pick a representative release for a release-group and return its
    tracklist as (disc, position, title), plus that release's MBID (for art).
    """
    rg = _mb_get(f"{_MB}/release-group/{rg_id}?inc=releases&fmt=json")
    if not rg:
        return [], ""
    releases = rg.get("releases", [])
    if not releases:
        return [], ""

    # Prefer the earliest official release with a sensible track count.
    def _key(r: dict) -> tuple:
        official = 0 if r.get("status") == "Official" else 1
        return (official, r.get("date") or "9999")

    for rel in sorted(releases, key=_key):
        rel_id = rel.get("id")
        if not rel_id:
            continue
        full = _mb_get(f"{_MB}/release/{rel_id}?inc=recordings+media&fmt=json")
        if not full:
            continue
        tracklist: list[tuple[int, int, str]] = []
        for disc_i, medium in enumerate(full.get("media", []), start=1):
            for track in medium.get("tracks", []):
                pos = track.get("position")
                title = track.get("title") or (track.get("recording") or {}).get("title", "")
                if pos and title:
                    tracklist.append((disc_i, int(pos), title))
        if tracklist:
            return tracklist, rel_id
    return [], ""


# --- Cover Art Archive --------------------------------------------------------

def cover_front(mbid: str, kind: str = "release-group") -> bytes | None:
    """Fetch the front cover for a release or release-group MBID, or None."""
    if not mbid:
        return None
    for size in ("front-1200", "front"):
        url = f"{_CAA}/{kind}/{mbid}/{size}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except Exception:
            continue
    return None
