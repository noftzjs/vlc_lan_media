"""LAN Party Projector Queue.

A small FastAPI app that lets people on the LAN browse media from the NAS and
push it to the playlist of a VLC instance (running on this machine, hooked up
to the projector) through VLC's built-in HTTP interface.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")

# --- CONFIGURATION (override any of these in .env or as environment variables) ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Semicolon-separated list of folders to expose. Each folder becomes a "library"
# in the UI, labelled with its folder name.
MEDIA_DIRS = [
    Path(p.strip())
    for p in os.environ.get("MEDIA_DIRS", r"N:\TV Shows;N:\Movies;N:\Anime").split(";")
    if p.strip()
]
# Which library labels hold movies vs. TV shows (affects how posters are looked up).
# Anything else is searched as both.
MOVIE_LIBRARIES = {s.strip() for s in os.environ.get("MOVIE_LIBRARIES", "Movies").split(";")}
TV_LIBRARIES = {s.strip() for s in os.environ.get("TV_LIBRARIES", "TV Shows").split(";")}

VLC_HOST = os.environ.get("VLC_HOST", "127.0.0.1")
VLC_PORT = int(os.environ.get("VLC_PORT", "8080"))
VLC_PASSWORD = os.environ.get("VLC_PASSWORD", "Password123")
VLC_BASE_URL = f"http://{VLC_HOST}:{VLC_PORT}/requests/"

# Re-scan the NAS at most this often (seconds). A scan can take a while over the network.
SCAN_TTL = int(os.environ.get("SCAN_TTL", "600"))

# The Movie Database. Free key from https://www.themoviedb.org/settings/api
# Leave empty to run without posters.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
POSTER_DIR = BASE_DIR / "cache" / "posters"
POSTER_META_FILE = BASE_DIR / "cache" / "posters.json"
POSTER_RETRY_AFTER = 7 * 24 * 3600  # re-check titles with no poster found after a week

QUEUE_LOG = BASE_DIR / "cache" / "queue.log"  # who queued what, one line per request
TRACK_PREFS_FILE = BASE_DIR / "cache" / "track_prefs.json"  # remembered audio/sub choice per show

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".ts", ".mpg", ".mpeg"}


# --- TITLE PARSING ---
# Folder names look like "12 Strong (2018) [1080p]", "Atomic Blonde 2017 HD-TS x264-CPG",
# "Wonder Woman 1984", "Evangelion 1.0 You Are (2007)". Pull out a searchable title + year.
_QUALITY_RE = re.compile(
    r"\b(480p|720p|1080p|2160p|4k|uhd|hdr|hdr10|bluray|blu-ray|brrip|bdrip|webrip|web-dl|webdl|webscr|"
    r"hdtv|hd-ts|hdts|hdcam|cam|ts|dvdrip|dvdscr|x264|x265|h264|h265|hevc|aac|ac3|dts|remux|proper|"
    r"repack|extended|unrated|imax|multi|dubbed)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def parse_title(name: str) -> tuple[str, str, int | None]:
    """Return (title, cleaned_name, year). `cleaned_name` keeps the year, `title` drops it."""
    s = re.sub(r"\.(" + "|".join(e[1:] for e in VIDEO_EXTS) + r")$", "", name, flags=re.IGNORECASE)
    s = re.sub(r"\[[^\]]*\]", " ", s)  # [1080p]
    s = re.sub(r"\(((?:19|20)\d{2})\)", r"\1", s)  # (2018) -> 2018
    if " " not in s.strip() and "." in s:  # Dotted.Release.Names
        s = s.replace(".", " ")
    m = _QUALITY_RE.search(s)
    if m and m.start() > 0:
        s = s[: m.start()]
    cleaned = re.sub(r"\s+", " ", s).strip(" -_()")
    year = None
    title = cleaned
    m = _YEAR_RE.search(cleaned)
    if m and cleaned[: m.start()].strip(" -_("):  # don't treat "1917" as a year with no title
        year = int(m.group(1))
        title = re.sub(r"\s+", " ", cleaned[: m.start()]).strip(" -_(")
    return title or name, cleaned or name, year


_SXXEYY_RE = re.compile(r"\bS(\d{1,2})\s?E(\d{1,3})\b", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^(?:season|series|s)\s?(\d{1,2})\b", re.IGNORECASE)
# Openings/endings/bonus material. Deliberately not matching OVA/OAD (real episodes) or the
# singular "extra"/"special" (shows up in episode titles).
_EXTRA_RE = re.compile(
    r"creditless|textless|\bNC(?:OP|ED)?\d*\b|\bOP\d+\b|\bED\d+\b|\bextras\b|special features|"
    r"\bpreview\b|\btrailer\b|\bsample\b|\bbonus\b",
    re.IGNORECASE,
)
_NXNN_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")  # "01x05"
_EPISODE_RES = [
    re.compile(r"\bE(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\bEp(?:isode)?\.?\s?(\d{1,3})\b", re.IGNORECASE),
    re.compile(r" - (\d{1,3})(?:v\d+)?(?!\d)"),  # anime style: "Show - 01v2 (1080p)"
    re.compile(r"(?<!\d)(\d{1,3})(?!\d)"),  # last resort: first standalone number
]


def classify_episode(parts: tuple[str, ...]) -> tuple[int | None, str | None, int | None, bool]:
    """From the path parts below the show folder, return (season number, season label, episode number, is_extra).

    Handles "Season 1/x.mkv", "x.S01E02.mkv", "Show - 01x05 - Title.mkv",
    "Sub Folder/[Group] Show - 03 (1080p).mkv" and "Season 1/Extras/Creditless Opening.mkv".
    """
    filename = Path(parts[-1]).stem
    folders = parts[:-1]
    season_num = season_label = episode = None
    extra = bool(_EXTRA_RE.search(filename)) or any(_EXTRA_RE.search(f) for f in folders)
    m = _SXXEYY_RE.search(filename) or _NXNN_RE.search(filename)
    if m:
        season_num, episode = int(m.group(1)), int(m.group(2))
    for folder in folders:
        fm = _SEASON_DIR_RE.match(folder)
        if fm:
            season_num = season_num if season_num is not None else int(fm.group(1))
            season_label = folder
            break
    if season_num is not None and season_label is None:
        season_label = f"Season {season_num}"
    if episode is None:
        cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", filename)  # drop [Group] and (1080p)
        cleaned = re.sub(r"\b\d{3,4}p\b|\bx26[45]\b|\bh\.?26[45]\b|\b(19|20)\d{2}\b", " ", cleaned, flags=re.IGNORECASE)
        for rx in _EPISODE_RES:
            em = rx.search(cleaned)
            if em:
                episode = int(em.group(1))
                break
    return season_num, season_label, episode, extra


# --- LIBRARY SCANNING ---
class Library:
    """Thread-safe cache of the media files found under MEDIA_DIRS, grouped by movie/show."""

    def __init__(self, roots: list[Path]):
        self.roots: dict[str, Path] = {}
        for root in roots:
            label = root.name or str(root)
            n = 2
            while label in self.roots:  # two folders with the same name
                label, n = f"{root.name} ({n})", n + 1
            self.roots[label] = root
        self.groups: list[dict] = []
        self.by_relpath: dict[tuple[str, str], dict] = {}  # (label, "rel/path.mkv" lowercased) -> group
        self.queued_by: dict[tuple[str, str], str] = {}  # same key -> name of whoever queued it last
        self.scanned_at_by: dict[str, float] = {}  # label -> when that library was last scanned
        self.scanning_labels: set[str] = set()
        # VLC reports mapped drives as UNC paths (file://TRUENAS/Plex/Movies/...), so remember both spellings.
        self.root_prefixes: dict[str, set[str]] = {}
        for label, root in self.roots.items():
            spellings = {root.as_posix()}
            try:
                spellings.add(root.resolve().as_posix())
            except OSError:
                pass
            self.root_prefixes[label] = {s.lower().strip("/") + "/" for s in spellings}
        self._lock = threading.Lock()

    @property
    def scanning(self) -> bool:
        return bool(self.scanning_labels)

    @property
    def scanned_at(self) -> float | None:
        """Oldest library scan time, or None until every library has been scanned once."""
        if len(self.scanned_at_by) < len(self.roots):
            return None
        return min(self.scanned_at_by.values())

    @staticmethod
    def poster_url(label: str, group: str) -> str:
        return f"/api/poster/{quote(label, safe='')}/{quote(group, safe='')}"

    def scan(self, labels: set[str] | None = None) -> None:
        """Scan the given libraries (default: all) and swap in their groups, leaving the others untouched."""
        wanted = set(labels) if labels else set(self.roots)
        with self._lock:
            todo = wanted - self.scanning_labels  # don't double-scan a library already in progress
            if not todo:
                return
            self.scanning_labels |= todo
        try:
            started = time.time()
            groups: dict[tuple[str, str], dict] = {}
            by_relpath: dict[tuple[str, str], dict] = {}
            for label in sorted(todo):
                root = self.roots[label]
                if not root.is_dir():
                    log.warning("Media folder not found, skipping: %s", root)
                    continue
                for dirpath, _, files in os.walk(root):
                    for name in files:
                        if Path(name).suffix.lower() not in VIDEO_EXTS:
                            continue
                        rel = Path(dirpath, name).relative_to(root)
                        # Group by top-level folder ("12 Monkeys", "Top Gun (1986)"); loose files group by themselves.
                        group_name = rel.parts[0] if len(rel.parts) > 1 else rel.stem
                        g = groups.get((label, group_name))
                        if g is None:
                            title, _, year = parse_title(group_name)
                            g = groups[(label, group_name)] = {
                                "library": label,
                                "group": group_name,
                                "title": title,
                                "year": year,
                                "poster": self.poster_url(label, group_name),
                                "items": [],
                            }
                        season_num, season_label, episode, extra = classify_episode(rel.parts[1:] or rel.parts)
                        g["items"].append({
                            "id": f"{label}/{rel.as_posix()}",
                            "name": rel.as_posix(),
                            "file": name,
                            "season": season_label,
                            "_season_num": season_num,
                            "_parent": rel.parts[1] if len(rel.parts) > 2 else None,  # top folder under the show
                            "episode": episode,
                            "extra": extra,  # sorted last and left out of "Queue season"
                        })
                        by_relpath[(label, rel.as_posix().lower())] = g
            for g in groups.values():
                # Sub-folders holding several files act as seasons when the names gave no season
                # ("Code Geass/Lelouch of the Rebellion R2/ep.mkv"); single-file folders don't.
                parent_counts = {}
                for it in g["items"]:
                    parent_counts[it["_parent"]] = parent_counts.get(it["_parent"], 0) + 1
                for it in g["items"]:
                    if it["season"] is None and it["_parent"] and parent_counts[it["_parent"]] > 1:
                        it["season"] = it["_parent"]
                g["items"].sort(key=lambda x: (
                    x["_season_num"] if x["_season_num"] is not None else 10**6,
                    (x["season"] or "").lower(),
                    x["extra"],
                    x["episode"] if x["episode"] is not None else 10**6,
                    x["name"].lower(),
                ))
                for it in g["items"]:
                    del it["_season_num"], it["_parent"]
            with self._lock:
                kept = [g for g in self.groups if g["library"] not in todo]
                self.groups = sorted(kept + list(groups.values()), key=lambda g: (g["library"].lower(), g["title"].lower()))
                self.by_relpath = {k: v for k, v in self.by_relpath.items() if k[0] not in todo} | by_relpath
                now = time.time()
                for label in todo:
                    self.scanned_at_by[label] = now
            n_files = sum(len(g["items"]) for g in groups.values())
            log.info("Scanned %s: %d video files in %d titles in %.1fs", ", ".join(sorted(todo)), n_files, len(groups), time.time() - started)
        finally:
            with self._lock:
                self.scanning_labels -= todo
        posters.warm_in_background(self.groups)

    def scan_in_background(self, labels: set[str] | None = None) -> None:
        threading.Thread(target=self.scan, args=(labels,), name="library-scan", daemon=True).start()

    def stale_labels(self) -> set[str]:
        """Libraries never scanned, or scanned longer than SCAN_TTL ago."""
        now = time.time()
        return {label for label in self.roots if now - self.scanned_at_by.get(label, 0) > SCAN_TTL}

    def resolve(self, item_id: str) -> Path:
        """Turn a client-supplied id back into a real file, refusing anything outside the roots."""
        label, _, rel = item_id.partition("/")
        root = self.roots.get(label)
        if root is None or not rel:
            raise HTTPException(status_code=404, detail="Unknown library.")
        try:
            path = (root / rel).resolve(strict=True)
            path.relative_to(root.resolve())
        except (FileNotFoundError, ValueError, OSError):
            raise HTTPException(status_code=404, detail="File not found.")
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            raise HTTPException(status_code=400, detail="Not a video file.")
        return path

    def key_for_uri(self, uri: str) -> tuple[str, str] | None:
        """Map a file:// URI VLC reports (UNC or drive-letter form) back to our (label, relpath) key."""
        if not uri.startswith("file:"):
            return None
        u = urlparse(uri)
        path = unquote(u.path)
        full = f"//{u.netloc}{path}" if u.netloc else path.lstrip("/")  # UNC vs. drive letter
        full = full.replace("\\", "/").lower().strip("/")
        for label, prefixes in self.root_prefixes.items():
            for prefix in prefixes:
                if full.startswith(prefix):
                    key = (label, full[len(prefix):])
                    if key in self.by_relpath:
                        return key
        return None

    def key_for_id(self, item_id: str) -> tuple[str, str]:
        label, _, rel = item_id.partition("/")
        return (label, rel.lower())

    def describe(self, uri: str) -> dict:
        """Poster, title and who queued it, for a URI from VLC's playlist. Nones if it isn't ours."""
        key = self.key_for_uri(uri)
        if not key:
            return {"title": None, "poster": None, "queued_by": None}
        g = self.by_relpath[key]
        return {"title": g["title"], "poster": g["poster"], "queued_by": self.queued_by.get(key)}


# --- POSTERS (TMDB, cached on disk) ---
class Posters:
    def __init__(self):
        POSTER_DIR.mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, dict] = {}
        if POSTER_META_FILE.exists():
            try:
                self._meta = json.loads(POSTER_META_FILE.read_text("utf-8"))
            except ValueError:
                pass
        self._meta_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self.warming = False

    @staticmethod
    def _file_for(key: str) -> Path:
        return POSTER_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")

    def _save_meta(self) -> None:
        with self._meta_lock:
            POSTER_META_FILE.write_text(json.dumps(self._meta, indent=0), "utf-8")

    def _lock_for(self, key: str) -> threading.Lock:
        with self._meta_lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _tmdb(self, path: str, **params) -> dict:
        headers = {}
        if TMDB_API_KEY.startswith("eyJ"):  # v4 read access token
            headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
        else:
            params["api_key"] = TMDB_API_KEY
        r = requests.get("https://api.themoviedb.org/3/" + path, params=params, headers=headers, timeout=10)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "2")))
            r = requests.get("https://api.themoviedb.org/3/" + path, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def _search(self, library: str, group: str) -> str | None:
        """Return a TMDB poster_path for this title, or None."""
        title, cleaned, year = parse_title(group)
        if library in MOVIE_LIBRARIES:
            endpoint, year_param = "search/movie", "year"
        elif library in TV_LIBRARIES:
            endpoint, year_param = "search/tv", "first_air_date_year"
        else:
            endpoint, year_param = "search/multi", None
        # Most specific first; the raw name with the year in it catches "Wonder Woman 1984".
        attempts: list[dict] = []
        if year and year_param:
            attempts.append({"query": title, year_param: year})
        if cleaned != title:
            attempts.append({"query": cleaned})
        attempts.append({"query": title})
        for params in attempts:
            for res in self._tmdb(endpoint, **params).get("results", []):
                if res.get("media_type") == "person":
                    continue
                if res.get("poster_path"):
                    return res["poster_path"]
        return None

    def get(self, library: str, group: str) -> Path | None:
        """Cached poster file for a title, fetching it from TMDB on first use."""
        key = f"{library}/{group}"
        file = self._file_for(key)
        if file.exists():
            return file
        if not TMDB_API_KEY:
            return None
        with self._lock_for(key):
            if file.exists():
                return file
            entry = self._meta.get(key)
            if entry and not entry.get("poster_path") and time.time() - entry.get("checked", 0) < POSTER_RETRY_AFTER:
                return None  # known miss, don't hammer TMDB
            try:
                poster_path = entry["poster_path"] if entry and entry.get("poster_path") else self._search(library, group)
                self._meta[key] = {"poster_path": poster_path, "checked": time.time()}
                self._save_meta()
                if not poster_path:
                    return None
                img = requests.get(TMDB_IMAGE_BASE + poster_path, timeout=15)
                img.raise_for_status()
                tmp = file.with_suffix(".part")
                tmp.write_bytes(img.content)
                tmp.replace(file)
                return file
            except requests.RequestException as e:
                log.warning("Poster lookup failed for %s: %s", key, e)
                return None

    def warm_in_background(self, groups: list[dict]) -> None:
        """Pre-fetch posters for every title so the UI isn't waiting on TMDB."""
        if not TMDB_API_KEY or self.warming:
            return
        todo = [g for g in groups if not self._file_for(f"{g['library']}/{g['group']}").exists()]
        if not todo:
            return

        def run():
            self.warming = True
            try:
                started = time.time()
                found = 0
                with ThreadPoolExecutor(max_workers=3, thread_name_prefix="poster") as pool:
                    for result in pool.map(lambda g: self.get(g["library"], g["group"]), todo):
                        found += result is not None
                log.info("Poster warm-up: %d/%d titles found in %.0fs", found, len(todo), time.time() - started)
            finally:
                self.warming = False

        threading.Thread(target=run, name="poster-warm", daemon=True).start()


posters = Posters()
library = Library(MEDIA_DIRS)


# --- TRACK PREFERENCES ---
# VLC ignores audio-language / sub-language for a lot of MKVs (tested: both per-item options and
# the command-line flag), but selecting a track by id works. So when someone picks a track we
# remember the language per show, and a watcher thread re-applies it whenever a new episode of
# that show starts playing.
_LANG_RE = re.compile(r"\[([^\]]+)\]\s*$")  # "English 2.0 FLAC - [English]" -> "English"


def track_lang(name: str) -> str | None:
    m = _LANG_RE.search(name or "")
    return m.group(1).strip().lower() if m else None


class TrackPrefs:
    def __init__(self):
        self.prefs: dict[str, dict] = {}  # "Library/Group" -> {"audio": {...}|None, "subtitle": {...}|None}
        if TRACK_PREFS_FILE.exists():
            try:
                self.prefs = json.loads(TRACK_PREFS_FILE.read_text("utf-8"))
            except ValueError:
                pass
        self._lock = threading.Lock()

    def _save(self) -> None:
        TRACK_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACK_PREFS_FILE.write_text(json.dumps(self.prefs, indent=1), "utf-8")

    def get(self, key: str) -> dict:
        return self.prefs.get(key, {})

    def remember(self, key: str, kind: str, tracks: list[dict], track_id: int) -> None:
        chosen = next((t for t in tracks if t["id"] == track_id), None)
        if chosen is None:
            return
        same_kind = [t for t in tracks if t["id"] != -1]
        with self._lock:
            entry = self.prefs.setdefault(key, {})
            entry[kind] = {
                "id": track_id,
                "name": chosen["name"],
                "lang": track_lang(chosen["name"]),
                "index": next((i for i, t in enumerate(same_kind) if t["id"] == track_id), None),
            }
            self._save()

    def forget(self, key: str) -> None:
        with self._lock:
            if self.prefs.pop(key, None) is not None:
                self._save()

    @staticmethod
    def choose(tracks: list[dict], pref: dict) -> int | None:
        """Pick the track id in `tracks` that best matches a remembered choice."""
        if pref["id"] == -1:
            return -1  # "Disable"
        real = [t for t in tracks if t["id"] != -1]
        if not real:
            return None
        by_name = next((t for t in real if t["name"] == pref["name"]), None)
        if by_name:
            return by_name["id"]
        if pref.get("lang"):
            same = [t for t in real if track_lang(t["name"]) == pref["lang"]]
            plain = [t for t in same if "commentary" not in t["name"].lower()]
            if plain or same:
                return (plain or same)[0]["id"]
        if pref.get("index") is not None and pref["index"] < len(real):
            return real[pref["index"]]["id"]
        return None


track_prefs = TrackPrefs()


# --- VLC HTTP INTERFACE ---
def vlc(endpoint: str, **params) -> dict:
    """Call VLC's HTTP interface (requests/status.json or requests/playlist.json) and return the JSON."""
    try:
        r = requests.get(VLC_BASE_URL + endpoint, params=params, auth=("", VLC_PASSWORD), timeout=5)
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="VLC is not running or its web interface is disabled.")
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="VLC did not respond in time.")
    if r.status_code == 401:
        raise HTTPException(status_code=502, detail="VLC rejected the HTTP password.")
    if r.status_code == 404 and endpoint == "tracks.json":
        raise HTTPException(status_code=501, detail="Track switching needs VLC started by start.ps1 (custom HTTP folder).")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VLC returned HTTP {r.status_code}.")
    return r.json()


def vlc_playlist() -> list[dict]:
    """Flatten VLC's playlist tree into the ordered list of queued items."""
    tree = vlc("playlist.json")
    nodes = tree.get("children", [])
    playlist_node = next((n for n in nodes if n.get("name") == "Playlist"), nodes[0] if nodes else {})
    return [
        {
            "id": int(c["id"]),
            "name": c.get("name", ""),
            "uri": c.get("uri", ""),
            "duration": c.get("duration", -1),
            "current": c.get("current") == "current",
            **library.describe(c.get("uri", "")),
        }
        for c in playlist_node.get("children", [])
        if c.get("type") == "leaf"
    ]


def current_show_key() -> tuple[str | None, dict | None]:
    """("Library/Group", group) for whatever VLC is playing, or (None, None)."""
    s = vlc("status.json")
    if s.get("state") == "stopped":
        return None, None
    current = next((x for x in vlc_playlist() if x["current"]), None)
    key = library.key_for_uri(current["uri"]) if current else None
    if not key:
        return None, None
    g = library.by_relpath[key]
    return f"{g['library']}/{g['group']}", g


def track_watcher() -> None:
    """Re-apply the remembered audio/subtitle choice each time a new item starts playing."""
    handled_plid = None
    attempts = 0
    while True:
        time.sleep(1)
        try:
            s = vlc("status.json")
            plid = s.get("currentplid", -1)
            if s.get("state") == "stopped" or plid in (-1, handled_plid):
                continue
            key, g = current_show_key()
            prefs = track_prefs.get(key) if key else {}
            if not prefs:
                handled_plid, attempts = plid, 0
                continue
            tracks = vlc("tracks.json")
            if not any(t["id"] != -1 for t in tracks.get("audio", []) + tracks.get("subtitle", [])):
                attempts += 1  # demux not ready yet; give it a few seconds
                if attempts < 15:
                    continue
            for kind in ("audio", "subtitle"):
                pref = prefs.get(kind)
                if not pref:
                    continue
                want = TrackPrefs.choose(tracks.get(kind, []), pref)
                have = next((t["id"] for t in tracks.get(kind, []) if t["current"]), None)
                if want is not None and want != have:
                    vlc("tracks.json", command={"audio": "audio_track", "subtitle": "subtitle_track"}[kind], val=want)
                    log.info("Track watcher: %s -> %s for %s", kind, want, g["title"])
            handled_plid, attempts = plid, 0
        except HTTPException:
            pass  # VLC down or started without vlc_http; try again next tick
        except Exception as e:  # never let the watcher die
            log.warning("Track watcher error: %s", e)


# --- APP ---
@asynccontextmanager
async def lifespan(_: FastAPI):
    if not TMDB_API_KEY:
        log.warning("TMDB_API_KEY not set: posters disabled. See .env.example")
    library.scan_in_background()
    threading.Thread(target=track_watcher, name="track-watcher", daemon=True).start()
    yield


app = FastAPI(title="LAN Party Projector Queue", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class QueueRequest(BaseModel):
    id: str
    user: str = ""


class BatchQueueRequest(BaseModel):
    ids: list[str]
    user: str = ""


class ControlRequest(BaseModel):
    action: str


class TrackRequest(BaseModel):
    kind: str  # "audio" or "subtitle"
    id: int


class ReorderRequest(BaseModel):
    ids: list[int]


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/media")
def list_media(refresh: str = Query("", description="'all' to rescan every library, or one library's name")):
    """Every title on the NAS with its files. Served from cache; rescans in the background when stale."""
    if refresh:
        if refresh.lower() in ("1", "true", "all"):
            library.scan_in_background()
        elif refresh in library.roots:
            library.scan_in_background({refresh})
        else:
            raise HTTPException(status_code=404, detail=f"Unknown library '{refresh}'.")
    elif library.stale_labels():
        library.scan_in_background(library.stale_labels())
    return {
        "groups": library.groups,
        "libraries": list(library.roots),
        "scanned_at": library.scanned_at,
        "scanned_at_by": library.scanned_at_by,
        "scanning": library.scanning,
        "scanning_libraries": sorted(library.scanning_labels),
        "posters_enabled": bool(TMDB_API_KEY),
        "posters_warming": posters.warming,
    }


@app.get("/api/poster/{library_label}/{group}")
def poster(library_label: str, group: str):
    if library_label not in library.roots:
        raise HTTPException(status_code=404, detail="Unknown library.")
    file = posters.get(library_label, group)
    if not file:
        # no-store so the browser retries once the warm-up has fetched it
        raise HTTPException(status_code=404, detail="No poster.", headers={"Cache-Control": "no-store"})
    return FileResponse(file, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def enqueue(ids: list[str], user: str) -> dict:
    """Add files to the end of VLC's playlist. Starts playback from the first one if VLC is idle."""
    paths = [library.resolve(i) for i in ids]  # validate everything before touching VLC
    user = re.sub(r"\s+", " ", user).strip()[:40]
    was_idle = vlc("status.json").get("state") == "stopped"
    first_new_id = None
    for item_id, path in zip(ids, paths):
        # Path.as_uri() percent-encodes spaces etc. so VLC gets a valid file:// MRL.
        vlc("status.json", command="in_enqueue", input=path.as_uri())
        if first_new_id is None and was_idle:
            items = vlc_playlist()
            first_new_id = items[-1]["id"] if items else None
        library.queued_by[library.key_for_id(item_id)] = user or None
        log.info("Queued by %s: %s", user or "anonymous", item_id)
        try:
            with QUEUE_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{user or '-'}\t{item_id}\n")
        except OSError:
            pass
    what = paths[0].name if len(paths) == 1 else f"{len(paths)} episodes"
    if was_idle and first_new_id is not None:
        vlc("status.json", command="pl_play", id=first_new_id)
        return {"message": f"Now playing: {what}"}
    return {"message": f"Queued: {what}"}


@app.post("/api/queue")
def queue_media(req: QueueRequest):
    return enqueue([req.id], req.user)


@app.post("/api/queue/batch")
def queue_batch(req: BatchQueueRequest):
    """Queue several files in order, e.g. a whole season."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="Nothing to queue.")
    if len(req.ids) > 200:
        raise HTTPException(status_code=400, detail="That's too many at once (max 200).")
    return enqueue(req.ids, req.user)


@app.post("/api/queue/reorder")
def reorder_queue(req: ReorderRequest):
    """Reorder the upcoming items. VLC's HTTP API has no 'move', so we delete them and re-add in the new order."""
    state = vlc("status.json").get("state")
    items = vlc_playlist()
    current = next((i for i, x in enumerate(items) if x["current"]), None)
    upcoming = items if state == "stopped" or current is None else items[current + 1:]
    by_id = {x["id"]: x for x in upcoming}
    if sorted(req.ids) != sorted(by_id):
        raise HTTPException(status_code=409, detail="The queue changed, refresh and try again.")
    if [x["id"] for x in upcoming] != req.ids:
        for x in upcoming:
            vlc("status.json", command="pl_delete", id=x["id"])
        for item_id in req.ids:
            vlc("status.json", command="in_enqueue", input=by_id[item_id]["uri"])
    return {"message": "Queue reordered", "queue": vlc_playlist()}


@app.get("/api/status")
def status():
    """What VLC is doing right now plus the full queue."""
    s = vlc("status.json")
    meta = s.get("information", {}).get("category", {}).get("meta", {})
    queue = vlc_playlist()
    current = next((x for x in queue if x["current"]), None)
    return {
        "state": s.get("state"),
        "now_playing": meta.get("title") or meta.get("filename") or (current or {}).get("name"),
        "title": current["title"] if current else None,
        "poster": current["poster"] if current else None,
        "time": s.get("time", 0),
        "length": s.get("length", 0),
        "volume": s.get("volume", 0),
        "fullscreen": bool(s.get("fullscreen")),
        "queue": queue,
    }


CONTROL_COMMANDS = {
    "pause": "pl_pause",  # toggles play/pause
    "play": "pl_play",
    "stop": "pl_stop",
    "next": "pl_next",
    "previous": "pl_previous",
    "fullscreen": "fullscreen",  # toggles
}


@app.post("/api/control")
def control(req: ControlRequest):
    cmd = CONTROL_COMMANDS.get(req.action)
    if not cmd:
        raise HTTPException(status_code=400, detail=f"Unknown action '{req.action}'.")
    vlc("status.json", command=cmd)
    return {"message": "ok"}


@app.delete("/api/queue/{item_id}")
def remove_from_queue(item_id: int):
    vlc("status.json", command="pl_delete", id=item_id)
    return {"message": "Removed from queue"}


def tracks_with_pref() -> dict:
    t = vlc("tracks.json")
    key, g = current_show_key()
    p = track_prefs.get(key) if key else {}
    t["pref"] = {
        "title": g["title"] if g else None,
        "audio": p["audio"]["name"] if p.get("audio") else None,
        "subtitle": p["subtitle"]["name"] if p.get("subtitle") else None,
    }
    return t


@app.get("/api/tracks")
def tracks():
    """Audio and subtitle tracks of the playing file, the selected one flagged, plus what's remembered for the show."""
    return tracks_with_pref()


@app.post("/api/tracks")
def select_track(req: TrackRequest):
    """Switch a track now and remember the choice for the rest of this show."""
    cmd = {"audio": "audio_track", "subtitle": "subtitle_track"}.get(req.kind)
    if not cmd:
        raise HTTPException(status_code=400, detail="kind must be 'audio' or 'subtitle'.")
    before = vlc("tracks.json")
    vlc("tracks.json", command=cmd, val=req.id)
    key, g = current_show_key()
    if key and len(g["items"]) > 1:  # only worth remembering for shows, not one-off movies
        track_prefs.remember(key, req.kind, before.get(req.kind, []), req.id)
    time.sleep(0.3)  # VLC applies the change asynchronously
    return tracks_with_pref()


@app.delete("/api/tracks/pref")
def forget_track_pref():
    key, _ = current_show_key()
    if key:
        track_prefs.forget(key)
    return tracks_with_pref()
