"""
Audion Extractor — free, local audio extractor for video links.

Runs a small Flask app on 127.0.0.1, uses yt-dlp to fetch audio and the
FFmpeg binary bundled with imageio-ffmpeg to convert it. No accounts,
no API keys, no paid services.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp.utils import DownloadCancelled

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #

APP_NAME = "Audion Extractor"
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
TMP_DIR = BASE_DIR / ".tmp"
LIBRARY_FILE = BASE_DIR / "library.json"

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# Frontend sends one of these keys; the server maps it to an actual conversion.
FORMATS = {
    "mp3-320": {"ext": "mp3", "codec": "mp3", "quality": "320", "label": "MP3 320"},
    "mp3-192": {"ext": "mp3", "codec": "mp3", "quality": "192", "label": "MP3 192"},
    "mp3-128": {"ext": "mp3", "codec": "mp3", "quality": "128", "label": "MP3 128"},
    "m4a": {"ext": "m4a", "codec": "m4a", "quality": "0", "label": "M4A"},
    "opus": {"ext": "opus", "codec": "opus", "quality": "0", "label": "OPUS"},
    "wav": {"ext": "wav", "codec": "wav", "quality": "0", "label": "WAV"},
    "flac": {"ext": "flac", "codec": "flac", "quality": "0", "label": "FLAC"},
}

MEDIA_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".aac", ".webm"}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

_library_lock = threading.Lock()
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audion")


# --------------------------------------------------------------------------- #
# Library persistence
# --------------------------------------------------------------------------- #

def load_library() -> list[dict]:
    if not LIBRARY_FILE.exists():
        return []
    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_library(items: list[dict]) -> None:
    tmp = LIBRARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LIBRARY_FILE)


def get_library() -> list[dict]:
    with _library_lock:
        return load_library()


# --------------------------------------------------------------------------- #
# yt-dlp helpers
# --------------------------------------------------------------------------- #

def normalize_url(raw: str | None) -> str:
    url = (raw or "").strip().strip("<>\"'")
    if not url:
        raise ValueError("Paste a video link first.")
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            raise ValueError("That doesn't look like a valid link. It should start with http:// or https://")
    return url


def base_options() -> dict:
    return {
        "ffmpeg_location": FFMPEG_PATH,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "noplaylist": True,
    }


def make_ydl(extra: dict | None = None) -> yt_dlp.YoutubeDL:
    opts = base_options()
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def entry_url(entry: dict) -> str | None:
    url = entry.get("webpage_url") or entry.get("url")
    if url and re.match(r"^https?://", url, re.I):
        return url
    if entry.get("id") and (entry.get("ie_key") or "").lower() == "youtube":
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return None


def clean_error(exc: Exception) -> str:
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    message = re.sub(r"^ERROR:\s*", "", message)
    message = re.sub(r"\s+", " ", message)
    if not message:
        return "Something went wrong while processing this link."
    if "Unsupported URL" in message:
        return "This site isn't supported yet. Try another link, or update yt-dlp."
    if "Sign in" in message or "cookies" in message.lower():
        return "This video needs a signed-in session, so it can't be fetched anonymously."
    return message[:400]


# --------------------------------------------------------------------------- #
# Job pipeline
# --------------------------------------------------------------------------- #

def pick_output(job_dir: Path, target_ext: str) -> Path | None:
    files = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    if not files:
        return None
    preferred = [p for p in files if p.suffix.lower() == f".{target_ext}"]
    return max(preferred or files, key=lambda p: p.stat().st_size)


def unique_destination(filename: str) -> Path:
    dest = DOWNLOAD_DIR / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for index in range(1, 1000):
        candidate = DOWNLOAD_DIR / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return DOWNLOAD_DIR / f"{stem} {uuid.uuid4().hex[:6]}{suffix}"


def run_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return

    fmt = FORMATS[job["format"]]
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(data: dict) -> None:
        if job.get("cancelled"):
            raise DownloadCancelled("Cancelled")
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            job["status"] = "downloading"
            job["downloaded"] = downloaded
            job["total"] = total
            job["progress"] = round(downloaded / total * 100, 1) if total else None
            job["speed"] = data.get("speed")
            job["eta"] = data.get("eta")
        elif status == "finished":
            job["status"] = "converting"
            job["progress"] = 100
            job["speed"] = None
            job["eta"] = None

    def postprocessor_hook(data: dict) -> None:
        if job.get("cancelled"):
            raise DownloadCancelled("Cancelled")
        if data.get("status") == "started":
            job["status"] = "converting"

    options = {
        "format": "bestaudio/best",
        "paths": {"home": str(job_dir)},
        "outtmpl": {"default": "%(title).150B [%(id)s].%(ext)s"},
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt["codec"],
                "preferredquality": fmt["quality"],
            }
        ],
    }

    try:
        if job.get("cancelled"):
            raise DownloadCancelled("Cancelled")
        job["status"] = "starting"

        with make_ydl(options) as ydl:
            info = ydl.extract_info(job["url"], download=True)

        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None

        output = pick_output(job_dir, fmt["ext"])
        if not output:
            raise RuntimeError("Conversion finished but no audio file was produced.")

        job["title"] = job.get("title") or (info or {}).get("title") or output.stem
        job["thumbnail"] = job.get("thumbnail") or (info or {}).get("thumbnail")
        job["uploader"] = job.get("uploader") or (info or {}).get("uploader")
        if not job.get("duration"):
            job["duration"] = (info or {}).get("duration")
        job["webpage_url"] = job.get("webpage_url") or (info or {}).get("webpage_url") or job["url"]

        dest = unique_destination(output.name)
        shutil.move(str(output), str(dest))

        entry = {
            "id": uuid.uuid4().hex,
            "title": job["title"],
            "uploader": job.get("uploader"),
            "duration": job.get("duration"),
            "webpage_url": job.get("webpage_url"),
            "thumbnail": job.get("thumbnail"),
            "filename": dest.name,
            "ext": dest.suffix.lstrip(".").lower(),
            "size": dest.stat().st_size,
            "created_at": time.time(),
        }
        with _library_lock:
            items = load_library()
            items.insert(0, entry)
            save_library(items)

        job["status"] = "done"
        job["progress"] = 100
        job["file_id"] = entry["id"]
        job["filename"] = dest.name
        job["size"] = entry["size"]
        job["speed"] = None
        job["eta"] = None
        job["finished_at"] = time.time()

    except DownloadCancelled:
        job["status"] = "cancelled"
        job["speed"] = None
        job["eta"] = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        if job.get("cancelled"):
            job["status"] = "cancelled"
        else:
            job["status"] = "error"
            job["error"] = clean_error(exc)
        job["speed"] = None
        job["eta"] = None
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def create_job(url: str, fmt_key: str, meta: dict | None = None) -> dict:
    meta = meta or {}
    job = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "format": fmt_key,
        "title": meta.get("title"),
        "thumbnail": meta.get("thumbnail"),
        "uploader": meta.get("uploader"),
        "duration": meta.get("duration"),
        "status": "queued",
        "progress": 0,
        "downloaded": 0,
        "total": 0,
        "speed": None,
        "eta": None,
        "error": None,
        "file_id": None,
        "created_at": time.time(),
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    _executor.submit(run_job, job["id"])
    return job


def public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "url": job["url"],
        "format": job["format"],
        "format_label": FORMATS[job["format"]]["label"],
        "title": job.get("title") or job["url"],
        "thumbnail": job.get("thumbnail"),
        "uploader": job.get("uploader"),
        "duration": job.get("duration"),
        "status": job["status"],
        "progress": job.get("progress"),
        "downloaded": job.get("downloaded"),
        "total": job.get("total"),
        "speed": job.get("speed"),
        "eta": job.get("eta"),
        "error": job.get("error"),
        "file_id": job.get("file_id"),
        "filename": job.get("filename"),
        "size": job.get("size"),
        "created_at": job["created_at"],
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.post("/api/info")
def api_info():
    payload = request.get_json(silent=True) or {}
    try:
        url = normalize_url(payload.get("url"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    options = {
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlist_items": "1-50",
    }
    try:
        with make_ydl(options) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": clean_error(exc)}), 502

    if not data:
        return jsonify({"error": "Nothing was found at that link."}), 404

    if data.get("_type") == "playlist":
        entries = [
            {
                "title": entry.get("title"),
                "url": entry_url(entry),
                "duration": entry.get("duration"),
                "thumbnail": entry.get("thumbnail"),
                "uploader": entry.get("uploader") or data.get("uploader"),
            }
            for entry in (data.get("entries") or [])
            if entry and entry_url(entry)
        ]
        return jsonify(
            {
                "type": "playlist",
                "title": data.get("title") or "Playlist",
                "uploader": data.get("uploader"),
                "thumbnail": data.get("thumbnail") or (entries[0]["thumbnail"] if entries else None),
                "count": len(entries),
                "entries": entries,
            }
        )

    return jsonify(
        {
            "type": "video",
            "title": data.get("title") or "Untitled",
            "uploader": data.get("uploader"),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "webpage_url": data.get("webpage_url") or url,
            "extractor": data.get("extractor_key"),
        }
    )


@app.post("/api/extract")
def api_extract():
    payload = request.get_json(silent=True) or {}
    try:
        url = normalize_url(payload.get("url"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    fmt_key = payload.get("format") or "mp3-320"
    if fmt_key not in FORMATS:
        return jsonify({"error": "Unknown output format."}), 400

    options = {
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlist_items": "1-50",
    }
    try:
        with make_ydl(options) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": clean_error(exc)}), 502

    if not data:
        return jsonify({"error": "Nothing was found at that link."}), 404

    jobs: list[dict] = []
    if data.get("_type") == "playlist":
        for entry in (data.get("entries") or []):
            if not entry or not entry_url(entry):
                continue
            jobs.append(
                create_job(
                    entry_url(entry),
                    fmt_key,
                    {
                        "title": entry.get("title"),
                        "thumbnail": entry.get("thumbnail") or data.get("thumbnail"),
                        "uploader": entry.get("uploader") or data.get("uploader"),
                        "duration": entry.get("duration"),
                    },
                )
            )
        if not jobs:
            return jsonify({"error": "That playlist has no playable entries."}), 404
    else:
        jobs.append(
            create_job(
                data.get("webpage_url") or url,
                fmt_key,
                {
                    "title": data.get("title"),
                    "thumbnail": data.get("thumbnail"),
                    "uploader": data.get("uploader"),
                    "duration": data.get("duration"),
                },
            )
        )

    return jsonify({"jobs": [public_job(job) for job in jobs]})


@app.get("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)[:60]
    return jsonify({"jobs": [public_job(job) for job in jobs]})


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    job["cancelled"] = True
    if job["status"] == "queued":
        job["status"] = "cancelled"
    return jsonify({"ok": True})


@app.get("/api/library")
def api_library():
    items = get_library()
    with _jobs_lock:
        active = any(
            job["status"] in {"queued", "starting", "downloading", "converting"}
            for job in _jobs.values()
        )
    return jsonify({"items": items, "processing": active})


@app.get("/api/file/<file_id>")
def api_file(file_id: str):
    entry = next((item for item in get_library() if item["id"] == file_id), None)
    if not entry:
        return jsonify({"error": "File not found."}), 404
    path = DOWNLOAD_DIR / entry["filename"]
    if not path.exists():
        return jsonify({"error": "File is missing from disk."}), 404

    as_attachment = request.args.get("download") == "1"
    mimetype = {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "opus": "audio/ogg",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "webm": "audio/webm",
    }.get(entry["ext"], "application/octet-stream")

    return send_file(
        path,
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=entry["filename"],
        conditional=True,
    )


@app.delete("/api/library/<file_id>")
def api_delete(file_id: str):
    with _library_lock:
        items = load_library()
        entry = next((item for item in items if item["id"] == file_id), None)
        if not entry:
            return jsonify({"error": "File not found."}), 404
        path = (DOWNLOAD_DIR / entry["filename"]).resolve()
        try:
            path.relative_to(DOWNLOAD_DIR.resolve())
        except ValueError:
            return jsonify({"error": "Invalid file path."}), 400
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return jsonify({"error": f"Could not delete file: {exc}"}), 500
        items = [item for item in items if item["id"] != file_id]
        save_library(items)
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "app": APP_NAME,
            "ffmpeg": FFMPEG_PATH,
            "ytdlp": getattr(yt_dlp.version, "__version__", "unknown"),
            "downloads": str(DOWNLOAD_DIR),
        }
    )


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

def find_port(preferred: int = 5000) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    for leftover in TMP_DIR.iterdir():
        shutil.rmtree(leftover, ignore_errors=True)

    port = find_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  {APP_NAME} is running at {url}")
    print(f"  Downloads folder: {DOWNLOAD_DIR}")
    print("  Press Ctrl+C to stop.\n")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
