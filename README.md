# song-eater

Record audio from any app on macOS, split tracks on silence, identify songs via Now Playing metadata (with Shazam fallback), and save as tagged MP3s with album art.

## Prerequisites

- macOS 13+ (Ventura or later)
- Python 3.10–3.12

```bash
brew install ffmpeg
brew tap ungive/media-control && brew install media-control
```

## Install

```bash
make install
```

This compiles the ScreenCaptureKit audio capture helper and installs the Python package.

## Usage

```bash
# Capture from Chrome (default)
song-eater

# Capture from Spotify
song-eater -p Spotify

# Save to a specific folder
song-eater -o ~/Music/rips

# Manual mode: provide artist/album, tracks numbered sequentially
song-eater -a "Pink Floyd" -A "Dark Side of the Moon"

# Adjust silence detection
song-eater --threshold 0.005 --silence-duration 2.0
```

Start playing music, then run `song-eater`. It captures audio, splits on silence gaps (or Now Playing title changes), identifies each track, and saves as a tagged MP3 with album art. Press **Ctrl+C** to stop.

The first time you run it, macOS will ask for **Screen & System Audio Recording** permission for your terminal app.

## Fixing tags after the fact

```bash
# Clean up the tags of MP3s already in a folder
song-eater --retag              # current folder
song-eater --retag ~/Music/mix  # a specific folder

# Revert the last retag
song-eater --retag --undo
```

Retag groups the files into albums (plus loose singles), looks each up on MusicBrainz **and** iTunes, and picks the release that best explains the whole folder — **preferring the original studio album over compilations/live/greatest-hits, but the unified deluxe edition when the folder includes bonus tracks.** It fixes album, track/disc numbers, year, and cover art. It prints a plan and asks before writing; **only embedded tags change, files are never renamed.** The last run is reversible with `--retag --undo`.

If a local `claude` CLI is present, it does the **grouping** for the whole folder — deciding which songs form an album, folding in stragglers whose album tag is missing, and unifying standard/deluxe editions (e.g. pulling `logical` into `GUTS (spilled)`). Track numbers and art still come from real releases (MusicBrainz/iTunes), never the model, so the AI groups but never invents data. `--no-ai` disables it and falls back to grouping by the album tag; it's never required.

State (undo history, plus definitive art thumbnails from capture) lives in a hidden `.song-eater/` folder inside the target directory. Wiping the folder resets everything.

## Keyboard controls

| Key | Action |
|-----|--------|
| `↑` `↓` | Scroll track list |
| `Ctrl+C` | Stop recording and exit |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--process`, `-p` | `Chrome` | App to capture audio from |
| `--device`, `-d` | — | Audio input device (e.g. `BlackHole`) instead of app capture |
| `--output`, `-o` | current dir | Output directory for MP3 files |
| `--artist`, `-a` | — | Artist name (enables manual mode, skips identification) |
| `--album`, `-A` | — | Album name (manual mode) |
| `--threshold`, `-t` | `0.01` | RMS silence threshold for track splitting |
| `--silence-duration`, `-s` | `3.0` | Seconds of silence to trigger a split |
| `--sample-rate` | `48000` | Sample rate in Hz |

## How it works

- **Audio capture** uses macOS ScreenCaptureKit (13+) to tap system or per-app audio — no virtual audio drivers needed
- **Track splitting** detects silence gaps between songs, and also splits when macOS Now Playing reports a title change (handles crossfades)
- **Identification** polls macOS Now Playing metadata via `media-control` throughout recording, using majority vote across multiple polls to reject stale metadata. Falls back to Shazam fingerprinting if Now Playing is unavailable
- **Partial rejection** discards recordings under 80% of the expected song duration (or under 60s if duration is unknown). Snippets under 20s are silently dropped
- **Export** converts to 192k MP3 via ffmpeg, tagged with ID3 metadata (title, artist, album, track number, cover art)
- **TUI** shows a full-screen Rich display with dB-scaled VU meter, recording progress bar, scrollable track list, and identified song info

## License

MIT
