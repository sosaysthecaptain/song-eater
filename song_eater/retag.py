"""Retag pass: clean up embedded tags of MP3s already in a folder.

Groups files into albums (plus loose singles), resolves each against
MusicBrainz — preferring the original studio album over compilations — and
fixes album, track/disc numbers, year, album-artist, and cover art. Only
embedded ID3 tags change; files are never renamed. Runs as `song-eater --retag`.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import click
from mutagen.id3 import ID3, ID3NoHeaderError

from song_eater import export, itunes, llm, musicbrainz as mb

UNDO_FILE = ".song-eater-undo.json"

# our tag name -> ID3 frame key
_FRAMES = {
    "album": "TALB", "title": "TIT2", "artist": "TPE1", "album_artist": "TPE2",
    "track": "TRCK", "disc": "TPOS", "year": "TDRC",
}
# fields we're willing to rewrite from an album/single match
_CONTEXT_FIELDS = ("album", "album_artist", "track", "disc", "year")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

@dataclass
class TrackFile:
    path: Path
    tags: dict           # current text values for _FRAMES keys
    art_len: int         # bytes of largest embedded cover, 0 if none


def _read_str(id3: ID3, frame: str) -> str:
    v = id3.get(frame)
    if v is not None and getattr(v, "text", None):
        return str(v.text[0])
    return ""


def scan_folder(folder: Path) -> list[TrackFile]:
    out: list[TrackFile] = []
    for p in sorted(folder.glob("*.mp3")):
        try:
            id3 = ID3(str(p))
        except (ID3NoHeaderError, Exception):
            id3 = ID3()
        tags = {name: _read_str(id3, frame) for name, frame in _FRAMES.items()}
        art_len = max((len(id3[k].data) for k in id3.keys() if k.startswith("APIC")), default=0)
        out.append(TrackFile(path=p, tags=tags, art_len=art_len))
    return out


# --------------------------------------------------------------------------- #
# Normalization + matching
# --------------------------------------------------------------------------- #

_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_FEAT = re.compile(r"\s*(feat\.?|featuring)\s.*", re.I)
_DASHQUAL = re.compile(
    r"\s*-\s*(live|remaster(ed)?|mono|stereo|single version|album version|"
    r"remix|demo|acoustic|edit).*$",
    re.I,
)


def norm(s: str) -> str:
    s = (s or "").lower()
    s = _PAREN.sub("", s)
    s = _FEAT.sub("", s)
    s = _DASHQUAL.sub("", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


_EDITION = re.compile(
    r"\s*-\s*(deluxe|remaster(ed)?|expanded|anniversary|special|super deluxe).*$", re.I)


def clean_album(s: str) -> str:
    """Strip edition qualifiers so 'Rumours (Super Deluxe)' searches as 'Rumours'."""
    s = _PAREN.sub("", s or "")
    s = _EDITION.sub("", s)
    return s.strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    aw, bw = set(a.split()), set(b.split())
    return len(aw & bw) / max(len(aw | bw), 1)


_BAD_SECONDARY = {
    "Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street",
    "Demo", "Interview", "Audiobook", "Soundtrack", "Spokenword",
}


def _type_weight(primary: str, secondary: list[str]) -> float:
    if secondary and any(s in _BAD_SECONDARY for s in secondary):
        return 0.2
    return {"Album": 1.0, "EP": 0.85, "Single": 0.6}.get(primary, 0.4)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

@dataclass
class ReleaseMatch:
    album: str
    album_artist: str
    year: str
    mbid: str
    tracklist: list          # [(disc, pos, title)]
    confidence: str          # "confident" | "weak"
    art: tuple | None = None  # (bytes, mime, length)
    source: str = "musicbrainz"


def _primary_artist(tf: TrackFile) -> str:
    a = tf.tags["album_artist"] or tf.tags["artist"]
    return a.split(",")[0].strip()


@dataclass
class Candidate:
    source: str                 # "musicbrainz" | "itunes"
    album: str
    album_artist: str
    year: str
    tracklist: list             # [(disc, pos, title)]
    type_weight: float          # penalty for comps/live etc.
    mbid: str = ""
    art_url: str = ""


def _mb_candidates(artist: str, album: str) -> list[Candidate]:
    out = []
    for rg in mb.search_release_groups(artist, album)[:6]:
        primary, secondary = mb.release_group_kind(rg)
        tracklist, _ = mb.release_group_tracklist(rg["id"])
        if not tracklist:
            continue
        out.append(Candidate(
            "musicbrainz", rg.get("title", album),
            (rg.get("artist-credit") or [{}])[0].get("name", artist),
            mb.first_year(rg), tracklist, _type_weight(primary, secondary), mbid=rg["id"]))
    return out


def _itunes_candidates(artist: str, album: str) -> list[Candidate]:
    out = []
    for a in itunes.album_candidates(artist, album):
        out.append(Candidate(
            "itunes", a["album"], a["album_artist"], a["year"], a["tracklist"],
            0.4 if a["is_comp"] else 0.9,   # slightly below MB Album so MB originals win ties
            art_url=a["artwork_url"]))
    return out


def _score(cand: Candidate, folder_titles: set) -> tuple[float, float]:
    titles = {norm(t) for _, _, t in cand.tracklist}
    coverage = len(folder_titles & titles) / len(folder_titles) if folder_titles else 0.0
    year_bonus = (1 - min(int(cand.year or 3000), 3000) / 3000) * 0.05 if cand.year else 0
    return coverage, coverage * cand.type_weight + year_bonus


def resolve_album(files: list[TrackFile], use_ai: bool = True) -> ReleaseMatch | None:
    folder_titles = {norm(f.tags["title"]) for f in files if f.tags["title"]}
    if not folder_titles:
        return None
    artist = _primary_artist(files[0])
    album = clean_album(files[0].tags["album"])
    candidates = [c for c in _mb_candidates(artist, album) + _itunes_candidates(artist, album)
                  if c.tracklist]
    if not candidates:
        return None

    scored = sorted(((_score(c, folder_titles), c) for c in candidates), key=lambda x: -x[0][1])
    (best_cov, best_score), best = scored[0]
    if best_cov < 0.5:
        return None

    # AI tie-break: only when the top match is partial or a close call, and only
    # to CHOOSE among real candidates we fetched (numbers still come from data).
    if use_ai and llm.available():
        runner = scored[1][0][1] if len(scored) > 1 else 0.0
        if best_cov < 1.0 or (runner and best_score - runner < 0.1):
            picked = _ai_pick(folder_titles, [c for _, c in scored[:5]])
            if picked is not None:
                best = picked

    return _match_from_candidate(best, artist)


def _match_from_candidate(c: Candidate, artist: str) -> ReleaseMatch:
    art = None
    if c.source == "itunes" and c.art_url:
        b = itunes._fetch_artwork(c.art_url)
        if b:
            art = (b, "image/jpeg", len(b))
    return ReleaseMatch(
        album=c.album, album_artist=c.album_artist or artist, year=c.year,
        mbid=c.mbid, tracklist=c.tracklist, confidence="confident", art=art, source=c.source)


def _ai_pick(folder_titles: set, candidates: list[Candidate]) -> Candidate | None:
    """Ask the local model which candidate release best explains the folder.
    Returns a candidate only if the model is confident; else None (fall back)."""
    lines = []
    for i, c in enumerate(candidates):
        tl = ", ".join(t for _, _, t in c.tracklist)
        lines.append(f"[{i}] {c.source}: \"{c.album}\" ({c.year or '?'}, {len(c.tracklist)} tracks): {tl}")
    prompt = (
        "You are matching a folder of song files to the correct album release.\n"
        f"Folder song titles: {sorted(folder_titles)}\n\n"
        "Candidate releases:\n" + "\n".join(lines) + "\n\n"
        "Pick the ONE release that best explains ALL the folder songs — prefer the "
        "edition that contains every song (e.g. a deluxe) and, when songs also "
        "appear on compilations, prefer the original studio album.\n"
        "Return ONLY compact JSON: {\"index\": <int>, \"confidence\": <0..1>}."
    )
    result = llm.judge_json(prompt)
    if not isinstance(result, dict):
        return None
    idx, conf = result.get("index"), result.get("confidence", 0)
    if isinstance(idx, int) and 0 <= idx < len(candidates) and conf and conf >= 0.7:
        return candidates[idx]
    return None


def resolve_single(tf: TrackFile) -> ReleaseMatch | None:
    title, artist = tf.tags["title"], _primary_artist(tf)
    if not title or not artist:
        return None

    # iTunes reliably maps a mainstream song to its canonical home album
    # (album + track number + high-res art in one call) — better than MB for
    # loose singles, where MB's recording graph is noisy with live/comp versions.
    r = itunes.search(artist, title, "")
    if r and r.get("track_number"):
        art = None
        if r.get("artwork_data"):
            art = (r["artwork_data"], r.get("artwork_mime", "image/jpeg"), len(r["artwork_data"]))
        return ReleaseMatch(
            album=r.get("album", ""),
            album_artist=r.get("album_artist") or tf.tags["artist"],
            year=r.get("year") or "",
            mbid="",
            tracklist=[(int(r.get("disc_number") or 1), int(r["track_number"]), title)],
            confidence="confident",
            art=art,
            source="itunes",
        )

    # MB fallback: best title-matching recording, then its release-groups.
    nt = norm(title)
    recs = [r for r in mb.search_recordings(artist, title)
            if _sim(nt, norm(r.get("title", ""))) >= 0.6]
    if not recs:
        return None

    # Gather candidate release-groups (with type + date) from the top recordings.
    cands: dict[str, dict] = {}
    for rec in recs[:2]:
        for rg in mb.recording_release_groups(rec["id"]):
            cands.setdefault(rg["id"], rg)
    if not cands:
        return None

    def _rank(rg: dict) -> tuple:
        primary, secondary = mb.release_group_kind(rg)
        year = mb.first_year(rg) or "9999"
        return (_type_weight(primary, secondary), -int(year))  # best type, then earliest

    rg = max(cands.values(), key=_rank)
    primary, secondary = mb.release_group_kind(rg)
    tracklist, _ = mb.release_group_tracklist(rg["id"])
    pos = next(((d, p, t) for d, p, t in tracklist if _sim(nt, norm(t)) >= 0.6), None)
    conf = "confident" if _type_weight(primary, secondary) >= 0.85 and pos else "weak"
    return ReleaseMatch(
        album=rg.get("title", ""),
        album_artist=tf.tags["artist"],
        year=mb.first_year(rg),
        mbid=rg["id"],
        tracklist=[pos] if pos else [],
        confidence=conf,
    )


def assign_positions(files: list[TrackFile], tracklist: list) -> dict:
    """Map each file to its (disc, pos, title) in the chosen tracklist."""
    entries = [(i, d, p, t, norm(t)) for i, (d, p, t) in enumerate(tracklist)]
    used: set[int] = set()
    result: dict = {}
    # match strongest pairs first
    pairs = []
    for f in files:
        ft = norm(f.tags["title"])
        for i, d, p, t, nt in entries:
            s = _sim(ft, nt)
            if s >= 0.5:
                pairs.append((s, f.path, i, d, p, t))
    for s, path, i, d, p, t in sorted(pairs, key=lambda x: -x[0]):
        if path in result or i in used:
            continue
        result[path] = (d, p, t)
        used.add(i)
    return result


def fetch_art(match: ReleaseMatch) -> None:
    """Attach the best available cover art to the match (CAA, then iTunes).

    Whether it's actually written is decided per file in `_plan_file`, which
    only ever upgrades to a larger image.
    """
    if match.art:  # already carried in (e.g. iTunes single) — don't refetch
        return
    art = mb.cover_front(match.mbid, "release-group") if match.mbid else None
    mime = "image/jpeg"
    if not art and match.tracklist:
        res = itunes.search(match.album_artist, match.tracklist[0][2], match.album)
        if res and res.get("artwork_data"):
            art, mime = res["artwork_data"], res.get("artwork_mime", "image/jpeg")
    if art:
        match.art = (art, mime, len(art))


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #

@dataclass
class FilePlan:
    file: TrackFile
    match: ReleaseMatch | None
    pos: tuple | None                 # (disc, pos, title)
    changes: dict = field(default_factory=dict)   # tag -> (old, new)
    art_change: tuple | None = None   # (old_len, new_len) or None


def _plan_file(tf: TrackFile, match: ReleaseMatch | None, pos: tuple | None) -> FilePlan:
    plan = FilePlan(file=tf, match=match, pos=pos)
    if match is None or match.confidence == "weak":
        return plan  # leave weak / unmatched files untouched
    new = {
        "album": match.album,
        "album_artist": match.album_artist,
        "year": match.year,
    }
    if pos:
        new["disc"] = str(pos[0])
        new["track"] = str(pos[1])
    for tag, val in new.items():
        if val and val != tf.tags.get(tag, ""):
            plan.changes[tag] = (tf.tags.get(tag, ""), val)
    if match.art and match.art[2] > tf.art_len:   # only ever upgrade resolution
        plan.art_change = (tf.art_len, match.art[2])
    return plan


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def print_report(album_plans: list[tuple[ReleaseMatch, list[FilePlan]]],
                 loose_plans: list[FilePlan]) -> int:
    n_changed = 0
    if album_plans:
        click.echo(click.style("\n── ALBUMS ──────────────────────────────────", fg="cyan"))
    for match, plans in album_plans:
        head = f"{match.album_artist} — {match.album}"
        if match.year:
            head += f" ({match.year})"
        partial = match.confidence.startswith("partial")
        src = {"itunes": "iTunes", "musicbrainz": "MusicBrainz"}.get(match.source, match.source)
        click.echo(click.style(f"\n{head}", bold=True) +
                   click.style(f"   [{src} · {match.confidence}]",
                               fg="yellow" if partial else "bright_black"))
        if any(pl.art_change for pl in plans):
            click.echo("   " + click.style(f"art → {match.art[2] // 1024}KB", fg="green"))
        for pl in plans:
            _print_file_line(pl)
            if pl.changes or pl.art_change:
                n_changed += 1
    if loose_plans:
        click.echo(click.style("\n── LOOSE SONGS ─────────────────────────────", fg="cyan"))
        for pl in loose_plans:
            _print_loose_line(pl)
            if pl.changes or pl.art_change:
                n_changed += 1
    return n_changed


def _print_file_line(pl: FilePlan) -> None:
    tf = pl.file
    title = tf.tags["title"] or tf.path.name
    if pl.match and pl.pos is None:   # couldn't place in this edition
        click.echo(click.style(f"    –   {title}", fg="yellow")
                   + click.style("   couldn't place — bonus/deluxe edition? left as-is",
                                 fg="bright_black"))
        return
    if not pl.changes and not pl.art_change:
        click.echo(click.style(f"   ok   {title}", fg="bright_black"))
        return
    num = pl.changes.get("track")
    numstr = f"#{num[1]:>2}" if num else (f"#{pl.pos[1]:>2}" if pl.pos else "  ?")
    detail = []
    if "track" in pl.changes:
        detail.append(f"track {pl.changes['track'][0] or '—'}→{pl.changes['track'][1]}")
    if "album" in pl.changes:
        detail.append(f'album "{pl.changes["album"][1]}"')
    click.echo(f"   {click.style(numstr, fg='yellow')}  {title}"
               + (f"   {click.style(', '.join(detail), fg='bright_black')}" if detail else ""))


def _print_loose_line(pl: FilePlan) -> None:
    tf = pl.file
    label = f"{tf.tags['artist']} — {tf.tags['title']}".strip(" —") or tf.path.name
    if pl.match is None:
        click.echo(f"   {label}\n      {click.style('→ no confident match, leaving as-is', fg='bright_black')}")
        return
    where = pl.match.album + (f" ({pl.match.year})" if pl.match.year else "")
    trk = f", track {pl.pos[1]}" if pl.pos else ""
    tail = click.style(f"→ {where}{trk} · {pl.match.confidence}",
                       fg="green" if pl.match.confidence == "confident" else "bright_black")
    click.echo(f"   {label}\n      {tail}")


# --------------------------------------------------------------------------- #
# Apply / undo
# --------------------------------------------------------------------------- #

def apply_plans(folder: Path, plans: list[FilePlan]) -> int:
    undo: dict = {"version": 1, "entries": {}}
    written = 0
    for pl in plans:
        if not pl.changes and not pl.art_change:
            continue
        tf = pl.file
        rel = tf.path.name
        # capture old values for the tags we're about to touch
        entry = {tag: tf.tags.get(tag, "") for tag in pl.changes}
        updates = {tag: new for tag, (_, new) in pl.changes.items()}
        if pl.art_change and pl.match and pl.match.art:
            entry["art_b64"] = _current_art_b64(tf.path)
            updates["artwork_data"] = pl.match.art[0]
            updates["artwork_mime"] = pl.match.art[1]
        undo["entries"][rel] = entry
        try:
            export.retag(tf.path, updates)
            written += 1
        except Exception as e:
            click.echo(click.style(f"   ! failed on {rel}: {e}", fg="red"), err=True)
    if written:
        (folder / UNDO_FILE).write_text(json.dumps(undo))
    return written


def _current_art_b64(path: Path) -> str | None:
    try:
        id3 = ID3(str(path))
    except Exception:
        return None
    for k in id3.keys():
        if k.startswith("APIC"):
            return base64.b64encode(id3[k].data).decode()
    return None


def undo_last(folder: Path) -> int:
    undo_path = folder / UNDO_FILE
    if not undo_path.exists():
        click.echo("Nothing to undo here.")
        return 0
    data = json.loads(undo_path.read_text())
    restored = 0
    for rel, entry in data.get("entries", {}).items():
        p = folder / rel
        if not p.exists():
            continue
        updates = {tag: val for tag, val in entry.items() if tag != "art_b64"}
        strip_art = "art_b64" in entry and not entry["art_b64"]
        if entry.get("art_b64"):
            updates["artwork_data"] = base64.b64decode(entry["art_b64"])
            updates["artwork_mime"] = "image/jpeg"
        try:
            export.retag(p, updates)
            if strip_art:                       # we added art where there was none — remove it
                id3 = ID3(str(p))
                id3.delall("APIC")
                id3.save()
            restored += 1
        except Exception:
            pass
    undo_path.unlink(missing_ok=True)
    return restored


# --------------------------------------------------------------------------- #
# Entry point (called from cli.main when --retag is passed)
# --------------------------------------------------------------------------- #

def run(folder: Path, undo: bool = False, assume_yes: bool = False,
        dry_run: bool = False, use_ai: bool = True) -> None:
    folder = folder.resolve()
    if undo:
        n = undo_last(folder)
        click.echo(f"Restored {n} file{'s' if n != 1 else ''}.")
        return

    tracks = scan_folder(folder)
    if not tracks:
        click.echo(f"No MP3s in {folder}.")
        return

    albums, loose = cluster(tracks)
    click.echo(f"Scanning {len(tracks)} files… "
               f"{len(albums)} album group{'s' if len(albums) != 1 else ''}, "
               f"{len(loose)} loose. Looking up on MusicBrainz…")

    album_plans: list[tuple[ReleaseMatch, list[FilePlan]]] = []
    still_loose: list[TrackFile] = list(loose)
    if use_ai and llm.available():
        click.echo("AI assist: on (local claude)")
    for files in albums:
        match = resolve_album(files, use_ai=use_ai)
        if not match:
            still_loose.extend(files)   # couldn't nail it — treat as singles
            continue
        fetch_art(match)
        posmap = assign_positions(files, match.tracklist)
        plans = []
        for f in files:
            pos = posmap.get(f.path)
            if pos:
                plans.append(_plan_file(f, match, pos))
            else:
                # This edition doesn't contain the track — leave it untouched
                # and flag it, rather than half-tagging (album but no number).
                plans.append(FilePlan(file=f, match=match, pos=None))
        n_unplaced = sum(1 for p in plans if p.pos is None)
        # Confidence = completeness. A partial match is NOT confident.
        match.confidence = ("confident" if n_unplaced == 0
                            else f"partial · {n_unplaced} unplaced")
        album_plans.append((match, plans))

    loose_plans: list[FilePlan] = []
    for tf in still_loose:
        match = resolve_single(tf)
        if match:
            fetch_art(match)
            pos = match.tracklist[0] if match.tracklist else None
            loose_plans.append(_plan_file(tf, match, pos))
        else:
            loose_plans.append(FilePlan(file=tf, match=None, pos=None))

    n_changed = print_report(album_plans, loose_plans)
    all_plans = [pl for _, plans in album_plans for pl in plans] + loose_plans
    click.echo(click.style(
        f"\n{n_changed} file{'s' if n_changed != 1 else ''} to update.", bold=True))

    if n_changed == 0 or dry_run:
        return
    if not assume_yes and not click.confirm("\nApply these changes?", default=False):
        click.echo("No changes made.")
        return
    written = apply_plans(folder, all_plans)
    click.echo(f"Updated {written} files. Undo with:  song-eater --retag --undo")


def cluster(tracks: list[TrackFile]) -> tuple[list[list[TrackFile]], list[TrackFile]]:
    groups: dict[tuple, list[TrackFile]] = {}
    for tf in tracks:
        artist = tf.tags["album_artist"] or tf.tags["artist"]
        groups.setdefault((norm(artist), norm(tf.tags["album"])), []).append(tf)
    albums, loose = [], []
    for (_, album_norm), files in groups.items():
        if album_norm and album_norm != "unknown" and len(files) >= 2:
            albums.append(files)
        else:
            loose.extend(files)
    return albums, loose
