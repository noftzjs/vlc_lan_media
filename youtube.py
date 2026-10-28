"""YouTube support through yt-dlp: search, metadata, and downloading videos for VLC.

VLC 3 no longer ships a working YouTube script, and feeding it YouTube's separate video/audio
stream URLs gives picture but no sound (the DASH audio never decodes). So we download the video
with yt-dlp, which merges video + audio with ffmpeg into one file in cache/youtube/, and queue
that like any other file. Short clips take a few seconds. yt-dlp needs a JavaScript runtime for
YouTube's checks; start.ps1 drops a portable Deno in tools/deno.

``Downloader`` is the queue's download worker (docs/queue_rebuild.md): it fetches the queue's
pending YouTube entries in queue order, two at a time, uses a video already in the cache
without calling yt-dlp, and reports each entry ready or failed back to the queue.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
# The same ladder capped at 720p: music videos and anything from a playlist, where the wait
# matters more than the pixels on a projector across the room.
FORMAT_CAPPED = os.environ.get(
    "YT_FORMAT_CAPPED",
    "bestvideo[vcodec^=avc1][width<=720][height>720]+bestaudio[ext=m4a]/"
    "bestvideo[vcodec^=avc1][height<=720]+bestaudio[ext=m4a]/"
    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]/best",
)
SEARCH_RESULTS = int(os.environ.get("YT_SEARCH_RESULTS", "10"))
CACHE_GB = float(os.environ.get("YT_CACHE_GB", "20"))  # trim oldest downloads beyond this
CACHE_DAYS = float(os.environ.get("YT_CACHE_DAYS", "7"))  # and anything older than this

_URL_RE = re.compile(r"(https?://)?(www\.|m\.|music\.)?(youtube\.com/|youtu\.be/)", re.IGNORECASE)


def available() -> bool:
    return bool(YTDLP)


def is_url(text: str) -> bool:
    return bool(_URL_RE.search(text.strip()))


_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
PLAYLIST_MAX = int(os.environ.get("YT_PLAYLIST_MAX", "25"))  # videos taken from a playlist, in order


def is_playlist(text: str) -> bool:
    """Any link carrying a playlist id: /playlist?list=..., a watch link with list=, or a Mix
    (list=RD..., YouTube's auto-generated radio). A watch link still lists its own video first."""
    t = text.strip()
    return is_url(t) and bool(_LIST_RE.search(t))


def playlist(url: str, limit: int = PLAYLIST_MAX) -> dict:
    """The first `limit` videos of a playlist in playlist order, with its title and full count.
    A Mix has no fixed length (YouTube keeps generating it), so its count is what we took."""
    import json

    url = url.strip()
    m = _LIST_RE.search(url)
    if not m:
        raise HTTPException(status_code=400, detail="That link has no playlist id.")
    list_id = m.group(1)
    # A Mix is only viewable through the watch link that seeded it, and a watch+list link works
    # for ordinary playlists too; only a bare id needs the playlist page.
    target = url if "v=" in url else f"https://www.youtube.com/playlist?list={list_id}"
    # --yes-playlist overrides the --no-playlist that _base_cmd sets for single videos.
    out = _run(["--yes-playlist", "--flat-playlist", "--playlist-end", str(limit), "-J", target], timeout=90)
    data = json.loads(out or "{}")
    entries = [_summary(e) for e in (data.get("entries") or []) if e and e.get("id")]
    is_mix = list_id.startswith("RD")
    return {"title": data.get("title") or ("YouTube Mix" if is_mix else "YouTube playlist"), "url": target,
            "count": len(entries) if is_mix else int(data.get("playlist_count") or len(entries)),
            "mix": is_mix, "entries": entries}


def _env() -> dict:
    """yt-dlp is a Python program: without this it writes titles in the console code page, so an
    é came through as a replacement character and 【brackets】 vanished before we read them."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


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
                           encoding="utf-8", errors="replace", timeout=timeout, env=_env(), check=False)
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


def is_music(url: str) -> bool:
    """YouTube files the video under the Music category. One quick metadata call, no download."""
    try:
        out = _run(["--skip-download", "--print", "%(categories)s", url.strip()], timeout=45)
    except HTTPException:
        return False
    return "music" in out.lower()


def download(url: str, on_progress: Callable[[dict], None] | None = None, timeout: int = 1800,
             handle: dict | None = None, cap: str | None = None) -> dict:
    """Download (or reuse from cache) one video merged into a single file. Returns metadata + 'path'
    + 'quality' ("1080p", or "720p (music)" / "720p (playlist)" when capped).

    `cap` names a reason to use the 720p ladder ("playlist"); with none given, a video YouTube
    files under Music is capped too. on_progress is called with {"percent": float, "eta": str}
    as the download runs. If `handle` is given, handle["proc"] is set to the yt-dlp process so a
    caller can cancel() it.
    """
    if not YTDLP:
        raise HTTPException(status_code=501, detail="yt-dlp is not installed (pip install yt-dlp[default]).")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cap is None and is_music(url):
        cap = "music"
    cmd = _base_cmd() + [
        "-f", FORMAT_CAPPED if cap else FORMAT, "--merge-output-format", "mkv",
        "-o", str(CACHE_DIR / "%(id)s.%(ext)s"),
        "--no-overwrites", "--continue",
        "--newline", "--progress", "--progress-template", f"download:{_PROGRESS}",
        "--print", f"before_dl:{_META}", "--print", "after_move:FILE\t%(filepath)s",
        url.strip(),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=_env())
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not start yt-dlp: {e}")
    if handle is not None:
        handle["proc"] = proc
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
    if handle is not None and handle.get("cancelled"):
        discard_partial(meta.get("id"))
        raise HTTPException(status_code=409, detail="Download cancelled.")
    if proc.returncode != 0 or not path or not Path(path).exists():
        raise _fail(stderr, "Download failed")
    meta["path"] = path
    meta["quality"] = f"720p ({cap})" if cap else "1080p"
    return meta


def cancel(handle: dict) -> bool:
    """Kill a running download (yt-dlp and the ffmpeg it spawned). Returns True if something was killed."""
    proc = handle.get("proc")
    if proc is None or proc.poll() is not None:
        return False
    handle["cancelled"] = True
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        proc.kill()
    return True


def discard_partial(video_id: str | None) -> None:
    """Remove the leftovers (.part, .ytdl, separate streams) of a cancelled download."""
    if not video_id or not CACHE_DIR.exists():
        return
    for f in CACHE_DIR.glob(f"{video_id}.*"):
        if f.suffix in (".part", ".ytdl") or ".f" in f.name:  # x.f137.mp4.part, x.f140.m4a, x.mkv.part
            try:
                f.unlink()
            except OSError:
                pass


def _path_key(path: str | Path) -> str:
    return str(path).replace("\\", "/").lower().strip("/")


def trim_cache(keep: set[str] = frozenset()) -> None:
    """Keep the download cache under CACHE_GB and drop files older than CACHE_DAYS.

    `keep` holds paths that must survive (anything still in the queue), in any case or
    slash style.
    """
    if not CACHE_DIR.exists():
        return
    keep = {_path_key(k) for k in keep}
    files = sorted((f for f in CACHE_DIR.iterdir() if f.is_file() and not f.name.endswith(".part")),
                   key=lambda f: f.stat().st_mtime)
    now = time.time()
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if _path_key(f) in keep:
            continue
        too_old = now - f.stat().st_mtime > CACHE_DAYS * 86400
        if too_old or total > CACHE_GB * 1e9:
            try:
                total -= f.stat().st_size
                f.unlink()
                log.info("Removed cached YouTube download %s", f.name)
            except OSError:
                pass


# --- the queue's download worker ---------------------------------------------------------------
SLOTS = int(os.environ.get("YT_SLOTS", "2"))  # downloads running at once
_VID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
# what yt-dlp leaves while it works: x.mkv.part, x.f137.mp4, x.ytdl, x.temp.mkv
_PARTIAL_RE = re.compile(r"\.(part|ytdl)$|\.f\d+\.|\.temp\.")


def video_id_of(url: str) -> str | None:
    m = _VID_RE.search(url or "")
    return m.group(1) if m else None


def cached_file(video_id: str) -> str | None:
    """The finished download of this video, if the cache has one. Its time is touched so
    the 7-day trim counts from the last use, not the first download."""
    if not video_id or not CACHE_DIR.exists():
        return None
    for f in CACHE_DIR.iterdir():
        if f.is_file() and f.name.split(".", 1)[0] == video_id and not _PARTIAL_RE.search(f.name):
            try:
                os.utime(f)
            except OSError:
                pass
            return str(f)
    return None


@dataclass
class Job:
    """One yt-dlp run. Entries queueing the same video share it."""

    key: str  # the video id (or the url, if no id could be read from it)
    url: str
    cap: str | None
    entry_ids: set[str]
    handle: dict = field(default_factory=dict)  # download() puts the process here; cancel() kills it
    percent: float = 0.0
    eta: str = ""


class Downloader:
    """Fetches the queue's pending YouTube entries, in queue order, ``slots`` at a time.

    Each tick looks at the queue afresh, so a reorder changes what starts next (nothing
    running is interrupted for it), and a download nothing queued needs any more (its
    entries were removed) is killed. An entry whose video is cached becomes ready at
    once, without yt-dlp. ``fetch``/``kill``/``cached``/``trim``/``spawn`` default to the
    real thing; tests pass fakes.
    """

    def __init__(
        self,
        queue,
        fetch: Callable[..., dict] | None = None,
        kill: Callable[[dict], bool] | None = None,
        cached: Callable[[str], str | None] | None = None,
        trim: Callable[[set[str]], None] | None = None,
        slots: int = SLOTS,
        on_finish: Callable[[list, dict | None, str | None], None] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ):
        self.queue = queue
        self.slots = slots
        self.on_finish = on_finish  # (entries, meta or None, error or None), for the request log
        self._fetch = fetch or download
        self._kill = kill or cancel
        self._cached = cached or cached_file
        self._trim = trim or trim_cache
        self._spawn = spawn or _thread
        self._lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()

    # --- the loop -------------------------------------------------------------------------
    def start(self, interval: float = 2.0) -> threading.Thread:
        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - one bad entry must not stop every download
                    log.warning("downloader tick failed: %s", e)
                self._wake.wait(interval)
                self._wake.clear()

        t = threading.Thread(target=run, name="yt-downloader", daemon=True)
        t.start()
        return t

    def wake(self) -> None:
        """Something was queued, moved or removed: look now rather than at the next tick."""
        self._wake.set()

    def stop_loop(self) -> None:
        self._stop.set()
        self._wake.set()

    def tick(self) -> None:
        with self._lock:
            self._drop_unneeded()
            for entry in self.queue.pending():
                if entry.kind != "youtube":
                    continue
                vid = entry.video_id or video_id_of(entry.url or "")
                key = vid or entry.url
                if not key:
                    self.queue.set_status(entry.id, "failed", error="No YouTube link for this entry")
                    continue
                job = self.jobs.get(key)
                if job is not None:  # the same video is already downloading: share it
                    job.entry_ids.add(entry.id)
                    self.queue.set_status(entry.id, "downloading")
                    continue
                path = self._cached(vid) if vid else None
                if path:
                    self.queue.set_status(entry.id, "ready", path=path)
                    continue
                if len(self.jobs) < self.slots:
                    self._begin(entry, key, vid)

    def progress(self) -> dict[str, dict]:
        """entry id -> {"percent", "eta"} for the entries downloading now."""
        with self._lock:
            return {
                eid: {"percent": job.percent, "eta": job.eta}
                for job in self.jobs.values()
                for eid in job.entry_ids
            }

    # --- internals ----------------------------------------------------------------------------
    def _begin(self, entry, key: str, vid: str | None) -> None:
        url = entry.url or f"https://www.youtube.com/watch?v={vid}"
        job = Job(key=key, url=url, cap=entry.cap, entry_ids={entry.id})
        self.jobs[key] = job
        self.queue.set_status(entry.id, "downloading")
        self._spawn(lambda: self._run(job))

    def _drop_unneeded(self) -> None:
        """Kill downloads whose entries have all left the queue (removed by someone)."""
        for key, job in list(self.jobs.items()):
            job.entry_ids = {i for i in job.entry_ids if self.queue.get(i) is not None}
            if not job.entry_ids:
                del self.jobs[key]
                self._kill(job.handle)
                log.info("Download no longer queued, stopped: %s", job.url)

    def _run(self, job: Job) -> None:
        def on_progress(p: dict) -> None:
            if "percent" in p:
                job.percent = float(p["percent"])
            if "eta" in p:
                job.eta = p["eta"]

        meta, error = None, None
        try:
            meta = self._fetch(job.url, on_progress, handle=job.handle, cap=job.cap)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the entries must hear of it
            error = str(getattr(e, "detail", "") or e or type(e).__name__)[:200]
        with self._lock:
            if self.jobs.get(job.key) is not job:
                return  # stopped by _drop_unneeded: nothing queued wants it any more
            del self.jobs[job.key]
            entries = []
            for eid in job.entry_ids:
                if meta:
                    extra = {k: meta[k] for k in ("duration", "thumbnail", "uploader", "quality") if meta.get(k)}
                    got = self.queue.set_status(eid, "ready", path=meta["path"], **extra)
                else:
                    got = self.queue.set_status(eid, "failed", error=error or "Download failed")
                if got is not None:
                    entries.append(got)
        if error:
            log.warning("YouTube download failed for %s: %s", job.url, error)
        if self.on_finish and entries:
            self.on_finish(entries, meta, error)
        if meta:
            try:
                self._trim(self.queue.in_use())
            except OSError as e:
                log.warning("cache trim failed: %s", e)
        self.wake()  # a slot is free


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="yt-download", daemon=True).start()
