# song-eater

Record system audio, split tracks on silence, identify songs, and save as tagged MP3s.

## Prerequisites

Requires **Python 3.10–3.12** (pydub/shazamio don't yet support 3.13+).

```bash
brew install blackhole-2ch ffmpeg python@3.12
```

After installing BlackHole, open **Audio MIDI Setup** on your Mac:
1. Click **+** in the bottom left, choose **Create Multi-Output Device**
2. Check both your speakers/headphones and **BlackHole 2ch**
3. Set this Multi-Output Device as your system output

This routes audio to both your speakers and BlackHole simultaneously.

## Install

```bash
pip install .
```

## Usage

```bash
# Auto mode: records, identifies songs via Shazam, tags, saves MP3s
song-eater

# Manual mode: provide artist/album, tracks numbered sequentially
song-eater --artist "Pink Floyd" --album "Dark Side of the Moon"

# Specify output directory
song-eater -o ~/Music/rips

# Adjust silence detection
song-eater --threshold 0.005 --silence-duration 2.0
```

Start playing music, then run `song-eater`. It listens for audio, splits on silence gaps, and saves each track as a tagged MP3. Press **Ctrl+C** to stop.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--device`, `-d` | `BlackHole` | Audio input device (substring match) |
| `--output`, `-o` | current dir | Output directory |
| `--artist`, `-a` | — | Artist name (enables manual mode) |
| `--album`, `-A` | — | Album name (manual mode) |
| `--threshold`, `-t` | `0.01` | RMS silence threshold |
| `--silence-duration`, `-s` | `3.0` | Seconds of silence to trigger split |
| `--sample-rate` | `44100` | Sample rate in Hz |

## License

MIT
