"""Perceptual image matching for album art.

Used to confirm a full-resolution candidate cover is the *same image* as the
definitive-but-tiny thumbnail macOS Now Playing gave us — so we only ever
upgrade art to a bigger copy of the cover we already know is correct, never
swap in a plausible-but-wrong one.
"""

from __future__ import annotations

import io

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # Pillow optional — callers fall back to size-only logic
    _HAVE_PIL = False


def available() -> bool:
    return _HAVE_PIL


def _dhash(data: bytes, size: int = 8) -> int | None:
    """64-bit difference hash. Scale-invariant, so a 300px thumbnail and a
    1200px cover of the same image hash to nearly the same value."""
    if not _HAVE_PIL:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize(
            (size + 1, size), Image.LANCZOS)
    except Exception:
        return None
    bits = 0
    idx = 0
    px = img.load()
    for y in range(size):
        for x in range(size):
            bits |= (1 if px[x, y] < px[x + 1, y] else 0) << idx
            idx += 1
    return bits


def same_cover(a: bytes | None, b: bytes | None, max_distance: int = 12) -> bool:
    """True if two encoded images are perceptually the same cover.

    Returns False (rather than raising) if Pillow is missing or either image
    can't be decoded — callers treat that as "can't verify".
    """
    ha, hb = _dhash(a) if a else None, _dhash(b) if b else None
    if ha is None or hb is None:
        return False
    return bin(ha ^ hb).count("1") <= max_distance
