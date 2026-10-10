"""YouTube support through yt-dlp: search, metadata, and downloading videos for VLC.

VLC 3 no longer ships a working YouTube script, and feeding it YouTube's separate video/audio
stream URLs gives picture but no sound (the DASH audio never decodes). So we download the video
with yt-dlp, which merges video + audio with ffmpeg into one file in cache/youtube/, and queue
that like any other file. Short clips take a few seconds. yt-dlp needs a JavaScript runtime for
YouTube's checks; start.ps1 drops a portable Deno in tools/deno.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

log = logging.getLogger("uvicorn.error")
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "youtube"

_ytdlp = os.environ.get("YTDLP_PATH") or str(Path(sys.executable).parent / "yt-dlp.exe")
YTDLP = _ytdlp if Path(_ytdlp).exists() else (shutil.which("yt-dlp") or "")
_deno = os.environ.get("DENO_PATH") or str(BASE_DIR / "tools" / "deno" / "deno.exe")
DENO = _deno if Path(_deno).exists() else shutil.which("deno")
FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")

# H.264 up to 1080p + AAC (light to decode, plays everywhere), else any mp4 pair, else whatever's best.
# The first clause only matches vertical video (Shorts) so they get their full 1080x1920 instead of
# being capped at 608x1080 by the height rule.
FORMAT = os.environ.get(
    "YT_FORMAT",
    "bestvideo[vcodec^=avc1][width<=1080][height>1080]+bestaudio[ext=m4a]/"
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/"
    "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best",
)
SEARCH_RESULTS = int(os.environ.get("YT_SEARCH_RESULTS", "10"))
CACHE_GB = float(os.environ.get("YT_CACHE_GB", "20"))  # trim oldest downloads beyond this
CACHE_DAYS = float(os.environ.get("YT_CACHE_DAYS", "7"))  # and anything older than this

_URL_RE = re.compile(r"(https?://)?(www\.|m\.|music\.)?(youtube\.com/|youtu\.be/)", re.IGNORECASE)


def available() -> bool:
    return bool(YTDLP)


def is_url(text: str) -> bool:
    return bool(_URL_RE.search(text.strip()))


def _base_cmd() -> list[str]:
    cmd = [YTDLP, "--no-playlist", "--no-warnings"]
    if DENO:
        cmd += ["--js-runtimes", f"deno:{DENO}"]
    if FFMPEG:
        cmd += ["--ffmpeg-location", FFMPEG]
    return cmd


def _fail(stderr: str, fallback: str = "yt-dlp failed") -> HTTPException:
    err = next((line for line in reversed(stderr.splitlines()) if line.strip()), fallback)
    log.warning("yt-dlp: %s", err)
    return HTTPException(status_code=502, detail=err.replace("ERROR: ", "")[:200])


def _run(args: list[str], timeout: int = 45) -> str:
    if not YTDLP:
        raise HTTPException(status_code=501, detail="yt-dlp is not installed (pip install yt-dlp[default]).")
    try:
        r = subprocess.run(_base_cmd() + ["--quiet"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="YouTube took too long to answer.")
    if r.returncode != 0:
        raise _fail(r.stderr)
    return r.stdout


def _thumbnail(entry: dict) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return thumbs[-1].get("url")
    return f"https://i.ytimg.com/vi/{entry['id']}/mqdefault.jpg" if entry.get("id") else None


def _summary(entry: dict) -> dict:
    vid = entry.get("id")
    return {
        "id": vid,
        "url": entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else entry.get("url")),
        "title": entry.get("title") or "",
        "uploader": entry.get("uploader") or entry.get("channel") or "",
        "duration": int(entry.get("duration") or 0),
        "thumbnail": _thumbnail(entry),
        "live": bool(entry.get("is_live")),
    }


def search(query: str, n: int = SEARCH_RESULTS) -> list[dict]:
    """Search results, or the single video a pasted link points to."""
    import json

    query = query.strip()
    if not query:
        return []
    target = query if is_url(query) else f"ytsearch{n}:{query}"
    out = _run(["--flat-playlist", "-j", target])
    results = []
    for line in out.splitlines():
        if line.strip():
            entry = json.loads(line)
            if entry.get("_type") in (None, "url", "video"):
                results.append(_summary(entry))
    return results


_META = "META\t%(id)s\t%(title)s\t%(duration)s\t%(uploader)s\t%(thumbnail)s\t%(is_live)s"
_PROGRESS = "PROGRESS\t%(progress._percent_str)s\t%(progress._eta_str)s"


def download(url: str, on_progress: Callable[[dict], None] | None = None, timeout: int = 1800) -> dict:
    """Download (or reuse from cache) one video merged into a single file. Returns metadata + 'path'.

    on_progress is called with {"percent": float, "eta": str} as the download runs.
    """
    if not YTDLP:
        raise HTTPException(status_code=501, detail="yt-dlp is not installed (pip install yt-dlp[default]).")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _base_cmd() + [
        "-f", FORMAT, "--merge-output-format", "mkv",
        "-o", str(CACHE_DIR / "%(id)s.%(ext)s"),
        "--no-overwrites", "--continue",
        "--newline", "--progress", "--progress-template", f"download:{_PROGRESS}",
        "--print", f"before_dl:{_META}", "--print", "after_move:FILE\t%(filepath)s",
        url.strip(),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not start yt-dlp: {e}")
    meta: dict = {}
    path = ""
    started = time.time()
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("META\t"):
            _, vid, title, duration, uploader, thumb, live = (line.split("\t") + [""] * 7)[:7]
            meta = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": title,
                    "uploader": "" if uploader == "NA" else uploader,
                    "duration": int(float(duration)) if duration not in ("NA", "") else 0,
                    "thumbnail": None if thumb == "NA" else thumb, "live": live == "True"}
            if on_progress:
                on_progress({"percent": 0.0, "eta": "", "meta": meta})
        elif line.startswith("PROGRESS\t") and on_progress:
            _, pct, eta = (line.split("\t") + ["", ""])[:3]
            try:
                on_progress({"percent": float(pct.strip().rstrip("%")), "eta": eta.strip()})
            except ValueError:
                pass
        elif line.startswith("FILE\t"):
            path = line[5:].strip()
        if time.time() - started > timeout:
            proc.kill()
            raise HTTPException(status_code=504, detail="Download took too long.")
    stderr = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0 or not path or not Path(path).exists():
        raise _fail(stderr, "Download failed")
    meta["path"] = path
    return meta


def trim_cache(keep: set[str] = frozenset()) -> None:
    """Keep the download cache under CACHE_GB and drop files older than CACHE_DAYS.

    `keep` holds lowercased paths that must survive (anything still in VLC's playlist).
    """
    if not CACHE_DIR.exists():
        return
    files = sorted((f for f in CACHE_DIR.iterdir() if f.is_file() and not f.name.endswith(".part")),
                   key=lambda f: f.stat().st_mtime)
    now = time.time()
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if str(f).replace("\\", "/").lower() in keep:
            continue
        too_old = now - f.stat().st_mtime > CACHE_DAYS * 86400
        if too_old or total > CACHE_GB * 1e9:
            try:
                total -= f.stat().st_size
                f.unlink()
                log.info("Removed cached YouTube download %s", f.name)
            except OSError:
                pass
