"""WAV to MP3 conversion and ID3 tagging."""

import re
import subprocess
import urllib.request
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK


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

    cover_data = metadata.get("artwork_data") or _fetch_cover(metadata.get("cover_url"))
    if cover_data:
        mime = metadata.get("artwork_mime", "image/jpeg")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))

    tags.save()
    return mp3_path


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def _fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None
