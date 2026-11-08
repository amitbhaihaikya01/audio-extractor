# Audion Extractor

Free, local audio extractor. Paste a video link, pick a format, and get the audio.
No accounts, no API keys, no paid services, no uploads — everything runs on your machine.

![stack](https://img.shields.io/badge/Python-Flask-3776AB) ![stack](https://img.shields.io/badge/engine-yt--dlp%20%2B%20FFmpeg-7c5cff)

## Quick start (Windows)

1. Double-click **`run.bat`**.
   The first launch installs dependencies (Flask, yt-dlp, and a bundled FFmpeg build) and opens
   `http://127.0.0.1:5000` in your browser.
2. Paste a link and press **Extract audio**.
3. Play it in the built-in player or hit download. Files are also saved to `downloads/`.

## Manual start

```bash
pip install -r requirements.txt
python app.py
```

Optional: set `PYTHONIOENCODING=utf-8` if you see encoding warnings on Windows.

## Features

- Works with YouTube, Instagram, TikTok, X/Twitter, Facebook, SoundCloud, Vimeo, Reddit,
  Bandcamp, direct media URLs, and [hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- Output formats: MP3 (320/192/128 kbps), M4A, OPUS, WAV, FLAC.
- Live progress with speed and time remaining; cancel any running job.
- Playlist links are queued automatically (first 50 entries).
- Built-in library with a persistent audio player, seek bar, and per-track download/delete.
- Handles long titles, unicode names, and duplicate filenames safely.
- Everything is stored locally: `downloads/` holds the audio, `library.json` the index.

## How it works

```
Browser  ──►  Flask (127.0.0.1)  ──►  yt-dlp  ──►  FFmpeg  ──►  downloads/*.mp3
```

- `yt-dlp` resolves the page and downloads the best available audio stream.
- FFmpeg (shipped inside the `imageio-ffmpeg` package) converts it to your chosen format.
- Two extractions run at a time; the rest wait in the queue.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "This site isn't supported yet" | Update the engine: `python -m pip install -U yt-dlp` |
| A specific video fails | It may be private, age-restricted, region-locked, or require sign-in. Try another link. |
| Browser didn't open | Go to `http://127.0.0.1:5000` manually. The console prints the actual port. |
| Port 5000 is busy | The app automatically picks the next free port and prints it. |
| FFmpeg errors | Reinstall the bundled binary: `python -m pip install --force-reinstall imageio-ffmpeg` |
| No sound in the player | Check the file plays in another player; download it and inspect. |

## Notes

- Only download content you own or have permission to use. Respect each site's terms and local law.
- This app does not bypass DRM.
- The server binds to `127.0.0.1`, so it is not reachable from other devices on your network.

## Project layout

```
app.py              Flask server, extraction pipeline, job queue, library API
templates/index.html  Single-page UI (no build step, no CDN)
requirements.txt
run.bat             Windows one-click launcher
downloads/          Extracted audio (created on first run)
library.json        Metadata index (created after the first extraction)
```
