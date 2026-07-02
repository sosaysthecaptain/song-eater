# Tagging: second-pass reconciliation (parked idea)

Captured 2026-07-01 from a design discussion. Not yet built — this is a note for later.

## The problem with current tagging (live path)

Tagging works via `song_eater/itunes.py` during capture, but has two structural weaknesses:

1. **Wrong track numbers.** `_title_matches()` accepts substring matches
   (`"Love"` matches `"Love Song"`), and `search()` takes the *first* collection
   that yields any title match. So a deluxe / live / compilation release with
   different numbering can win. Per-track matching has no way to know it picked
   the wrong release.

2. **Low-res album art.** The `artworkUrl100` → `1200x1200bb` URL trick only works
   when `album_match` succeeds. On fallback it keeps the streaming service's
   embedded low-res cover (pulled from macOS Now Playing).

## The idea (Marc's "blunt hammer")

Do the reconciliation **after** a session, not during capture. At end-of-session
you have the *whole tracklist*, which as a set nails down the exact release far
better than any single track can. Group saved MP3s by album, resolve each album
to one canonical release using the full tracklist as a constraint (order + fuzzy
titles → assignment), then write correct track/disc numbers and one high-res
cover for the whole group.

## Hard constraints Marc set

- **Stupid simple / easy to use.** No ceremony.
- **Operates on whatever is in the output folder** (a re-runnable pass over an
  existing folder of MP3s — NOT wired into the live capture path).
- Keep it fully separate from capture so it can never regress reliability.

## Direction

- **MusicBrainz was tried before and "didn't get far" — reason unknown.** Revisit
  why before committing to it again. (Cover Art Archive is the MB art source.)
- **AI path (Claude):** hand it the session's tracklist + current tags, let it
  disambiguate the release / sanity-check coherence / flag oddities, then fetch
  art + numbers deterministically. Better on messy cases (classical, features,
  mis-tags); adds a dependency + API key.
- Lean at time of discussion: build it as a separate, re-runnable step; only add
  it if it's genuinely low-friction.

## Status: NOT STARTED. Priority given to fixing the mid-song audio drop (#1).
