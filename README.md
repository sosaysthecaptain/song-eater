# song-eater

Record audio from any app, split tracks on silence, identify songs, and save as tagged MP3s.

## Prerequisites

```bash
brew install ffmpeg
```

## Install

```bash
make install
```

(This compiles the audio capture helper and installs the Python package.)

## Usage

```bash
# Capture from Chrome (or any app)
song-eater --process Chrome

# Capture from Spotify
song-eater --process Spotify

# Manual mode: provide artist/album, tracks numbered sequentially
song-eater -p Chrome --artist "Pink Floyd" --album "Dark Side of the Moon"

# Save to a specific folder
song-eater -p Spotify -o ~/Music/rips

# Adjust silence detection
song-eater -p Chrome --threshold 0.005 --silence-duration 2.0
```

Start playing music, then run `song-eater`. It captures audio from the specified app, splits on silence gaps, and saves each track as a tagged MP3. Press **Ctrl+C** to stop.

The first time you use `--process`, macOS will ask for audio capture permission.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--process`, `-p` | — | App to capture audio from (e.g. `Chrome`, `Spotify`) |
| `--device`, `-d` | — | Audio input device (e.g. `BlackHole`) |
| `--output`, `-o` | current dir | Output directory |
| `--artist`, `-a` | — | Artist name (enables manual mode) |
| `--album`, `-A` | — | Album name (manual mode) |
| `--threshold`, `-t` | `0.01` | RMS silence threshold |
| `--silence-duration`, `-s` | `3.0` | Seconds of silence to trigger split |
| `--sample-rate` | `44100` | Sample rate in Hz |

If neither `--process` nor `--device` is given, it falls back to looking for a BlackHole audio device.

## How it works

- **`--process`** uses the macOS CoreAudio Taps API (14.4+) to capture audio from a specific app — no virtual audio drivers needed, no system sounds captured
- **`--device`** records from an audio input device (e.g. BlackHole for system-wide capture)
- Songs are identified via Shazam fingerprinting (auto mode) or numbered sequentially (manual mode)
- Tagged with ID3 metadata (title, artist, album, track number, cover art)

## License

MIT
