"""WAV to MP3 conversion and ID3 tagging."""

import re
import subprocess
import urllib.request
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TCOM, TDRC, TIT2, TPE1, TPE2, TRCK


def save_track(wav_path: str, metadata: dict, output_dir: Path) -> Path:
    """Convert WAV to tagged MP3 and save to output_dir. Returns the MP3 path."""
    artist = metadata.get("artist", "Unknown")
    title = metadata.get("title", "Unknown")
    filename = _sanitize(f"{artist} - {title}.mp3")
    mp3_path = output_dir / filename

    # Handle duplicate filenames
    counter = 1
    while mp3_path.exists():
        counter += 1
        filename = _sanitize(f"{artist} - {title} ({counter}).mp3")
        mp3_path = output_dir / filename

    # Convert WAV to MP3 via ffmpeg
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3_path)],
        capture_output=True,
        check=True,
    )

    # Tag
    tags = ID3(str(mp3_path))
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=metadata.get("album", "Unknown")))
    tags.add(TRCK(encoding=3, text=str(metadata.get("track", ""))))

    album_artist = metadata.get("album_artist")
    if album_artist:
        tags.add(TPE2(encoding=3, text=album_artist))

    composer = metadata.get("composer")
    if composer:
        tags.add(TCOM(encoding=3, text=composer))

    year = metadata.get("year")
    if year:
        tags.add(TDRC(encoding=3, text=year))

    cover_data = metadata.get("artwork_data") or _fetch_cover(metadata.get("cover_url"))
    if cover_data:
        mime = metadata.get("artwork_mime", "image/jpeg")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))

    tags.save()
    return mp3_path


def _sanitize(name: str, max_bytes: int = 250) -> str:
    """Sanitize a filename and truncate to fit within filesystem byte limits.

    APFS allows 255 bytes per filename component.  We leave a small margin
    and truncate the *stem* (preserving the extension) when needed.
    """
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()

    # Split off extension so we never truncate it
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = ext, ""
    else:
        ext = "." + ext

    max_stem_bytes = max_bytes - len(ext.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) <= max_stem_bytes:
        return stem + ext

    # Truncate by decoding back from a byte slice (safe for multi-byte chars)
    truncated = encoded[:max_stem_bytes].decode("utf-8", errors="ignore").rstrip()
    return truncated + ext


def _fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None
