"""LAN Party Projector Queue.

A small FastAPI app that lets people on the LAN browse media from the NAS and
push it to the playlist of a VLC instance (running on this machine, hooked up
to the projector) through VLC's built-in HTTP interface.
"""

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import youtube

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

# Who queued what: one JSON object per line, private, reviewed after the party (tools/queue_report.py).
EVENT_LOG = BASE_DIR / "cache" / "queue_events.jsonl"
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
        """Poster, title, episode marker and who queued it, for a URI from VLC's playlist. Nones if it isn't ours."""
        key = self.key_for_uri(uri)
        if not key:
            return {"title": None, "poster": None, "queued_by": None, "episode": None}
        g = self.by_relpath[key]
        return {"title": g["title"], "poster": g["poster"], "queued_by": self.queued_by.get(key),
                "episode": self.episode_label(g, key)}

    @staticmethod
    def episode_label(g: dict, key: tuple[str, str]) -> str | None:
        """What tells one queued episode from the next: 'S02E05' when the folder names a season,
        'E05' otherwise, the file's own name for extras and unnumbered files. None for a one-file
        movie, whose title already says everything."""
        if len(g["items"]) < 2:
            return None
        wanted = f"{key[0]}/{key[1]}".lower()
        it = next((x for x in g["items"] if x["id"].lower() == wanted), None)
        if it is None:
            return None
        if it.get("episode") is not None and not it.get("extra"):
            m = _SEASON_DIR_RE.match((it.get("season") or "").strip())
            return (f"S{int(m.group(1)):02d}" if m else "") + f"E{it['episode']:02d}"
        return Path(it["file"]).stem


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


# --- EXTRA ITEMS IN THE QUEUE (YouTube downloads, uploaded images) ---
# These live under cache/; VLC only sees the path, so we keep title/thumbnail/options here.
extra_items: dict[str, dict] = {}  # lowercased local path -> {title, name, poster, queued_by, options, youtube?}
yt_jobs: dict[str, dict] = {}  # job id -> {url, title, user, percent, eta, status, error, started}
yt_lock = threading.Lock()


# --- QUEUE GROUPS (a season or a playlist queued in one go) AND WHO ASKED FOR WHAT ---
# VLC's item ids change every time the queue is rebuilt, so a group remembers its members by
# path; the page shows a contiguous run of one group as a single block that drags as a unit.
QUEUE_GROUPS_FILE = BASE_DIR / "cache" / "queue_groups.json"
queue_groups: dict[str, dict] = {}  # gid -> {label, kind, user, created, keys: [path key, ...]}
# Nobody logs in; the LAN address is the identity. A name typed once is remembered for it.
REQUESTERS_FILE = BASE_DIR / "cache" / "requesters.json"
requesters: dict[str, dict] = {}  # ip -> {name, seen}
groups_lock = threading.Lock()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def _save_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=0), "utf-8")
    except OSError as e:
        log.warning("Could not save %s: %s", path.name, e)


queue_groups.update(_load_json(QUEUE_GROUPS_FILE))
requesters.update(_load_json(REQUESTERS_FILE))


def new_group(label: str, kind: str, user: str | None) -> str:
    gid = hashlib.sha1(f"{label}{time.time()}".encode()).hexdigest()[:8]
    with groups_lock:
        queue_groups[gid] = {"label": label[:120], "kind": kind, "user": user, "created": time.time(), "keys": []}
        _save_json(QUEUE_GROUPS_FILE, queue_groups)
    return gid


def group_add(gid: str, uri: str) -> None:
    with groups_lock:
        g = queue_groups.get(gid)
        if g is None:
            return
        key = _path_key(uri)
        if key not in g["keys"]:
            g["keys"].append(key)
        _save_json(QUEUE_GROUPS_FILE, queue_groups)


def group_for_uri(uri: str) -> tuple[str | None, dict | None]:
    key = _path_key(uri)
    with groups_lock:
        for gid, g in reversed(list(queue_groups.items())):  # the newest group wins if a file is in two
            if key in g["keys"]:
                return gid, g
    return None, None


def prune_groups(items: list[dict]) -> None:
    """Forget groups with no member left in VLC's playlist."""
    present = {_path_key(x["uri"]) for x in items}
    with groups_lock:
        stale = [gid for gid, g in queue_groups.items() if not any(k in present for k in g["keys"])]
        for gid in stale:
            del queue_groups[gid]
        if stale:
            _save_json(QUEUE_GROUPS_FILE, queue_groups)


def client_ip(request: Request | None) -> str | None:
    return request.client.host if request is not None and request.client else None


def requester_name(ip: str | None, typed: str) -> str:
    """The name to credit: what was typed (remembered for this address from now on), else what
    this address called itself before, else nothing."""
    name = re.sub(r"\s+", " ", typed).strip()[:40]
    if not ip:
        return name
    with groups_lock:
        if name:
            requesters[ip] = {"name": name, "seen": time.time()}
            _save_json(REQUESTERS_FILE, requesters)
        else:
            name = (requesters.get(ip) or {}).get("name", "")
    return name


_PHONE_RE = re.compile(r"Mobile|Android|iPhone|iPad", re.IGNORECASE)


def device_kind(request: Request | None) -> str | None:
    """"phone" or "pc" from the user agent, for the after-party question of what people used."""
    if request is None:
        return None
    return "phone" if _PHONE_RE.search(request.headers.get("user-agent", "")) else "pc"


def queue_event(kind: str, *, name: str, ip: str | None, device: str | None = None, **fields) -> None:
    """Append one request to cache/queue_events.jsonl. `name` is the credited name (typed now or
    remembered for the address), empty when unknown; `fields` carry what was queued."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "name": name or None, "ip": ip,
           "device": device, **{k: v for k, v in fields.items() if v is not None}}
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Could not write the queue event log: %s", e)


def library_category(label: str) -> str:
    """movie / tv / anime / <library name>: the Anime folder is a TV library by env but worth its own bucket."""
    if "anime" in label.lower():
        return "anime"
    if label in MOVIE_LIBRARIES:
        return "movie"
    if label in TV_LIBRARIES:
        return "tv"
    return label.lower()
EXTRA_ITEMS_FILE = BASE_DIR / "cache" / "extra_items.json"  # survives server restarts


def _load_extra_items() -> None:
    if not EXTRA_ITEMS_FILE.exists():
        return
    try:
        data = json.loads(EXTRA_ITEMS_FILE.read_text("utf-8"))
    except ValueError:
        return
    # Drop entries whose file is gone (cache trimmed) so the table doesn't grow forever.
    extra_items.update({k: v for k, v in data.items() if Path(k).exists()})


def _save_extra_items() -> None:
    try:
        EXTRA_ITEMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXTRA_ITEMS_FILE.write_text(json.dumps(extra_items, indent=0), "utf-8")
    except OSError as e:
        log.warning("Could not save extra items: %s", e)


def remember_extra_item(path: Path, info: dict) -> None:
    with yt_lock:
        extra_items[_path_key(str(path))] = info
        _save_extra_items()


_load_extra_items()
IMAGE_DIR = BASE_DIR / "cache" / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
IMAGE_MAX_MB = int(os.environ.get("IMAGE_MAX_MB", "25"))
IMAGE_KEEP_DAYS = float(os.environ.get("IMAGE_KEEP_DAYS", "2"))


def _path_key(path_or_uri: str) -> str:
    if path_or_uri.startswith("file:"):
        u = urlparse(path_or_uri)
        p = unquote(u.path)
        path_or_uri = f"//{u.netloc}{p}" if u.netloc else p.lstrip("/")
    return path_or_uri.replace("\\", "/").lower().strip("/")


def extra_lookup(uri: str) -> dict | None:
    with yt_lock:
        return extra_items.get(_path_key(uri))


def queued_paths() -> set[str]:
    try:
        return {_path_key(x["uri"]) for x in vlc_playlist() if x["uri"].startswith("file:")}
    except HTTPException:
        return set()  # VLC not up; nothing is queued then


def yt_trim_cache() -> None:
    """Trim old downloads, but never one that's still in VLC's playlist."""
    youtube.trim_cache(queued_paths())


def image_trim_cache() -> None:
    """Uploaded images are throwaway: drop them after IMAGE_KEEP_DAYS unless still queued."""
    keep = queued_paths()
    now = time.time()
    for f in IMAGE_DIR.iterdir():
        if f.is_file() and _path_key(str(f)) not in keep and now - f.stat().st_mtime > IMAGE_KEEP_DAYS * 86400:
            try:
                f.unlink()
            except OSError:
                pass


def enqueue_uri(uri: str) -> None:
    """in_enqueue that re-applies per-item VLC options (e.g. an image's display time)."""
    item = extra_lookup(uri)
    params = {"command": "in_enqueue", "input": uri}
    if item and item.get("options"):
        params["option"] = item["options"]
    vlc("status.json", **params)


def _ffprobe_gif(path: Path) -> tuple[float, int]:
    """(duration seconds, frame count) of a GIF, or (0, 1) if ffprobe can't tell."""
    try:
        ffprobe = shutil.which("ffprobe") or (str(Path(youtube.FFMPEG).with_name("ffprobe.exe")) if youtube.FFMPEG else "ffprobe")
        r = subprocess.run([ffprobe, "-v", "error",
                            "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames:format=duration",
                            "-of", "default=noprint_wrappers=1", str(path)], capture_output=True, text=True, timeout=60, check=False)
        kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        frames = int(kv.get("nb_read_frames", "1")) if kv.get("nb_read_frames", "1").isdigit() else 1
        duration = float(kv.get("duration", "0")) if kv.get("duration", "N/A") != "N/A" else 0.0
        return duration, frames
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0, 1


def normalize_image(src: Path, dest: Path) -> None:
    """Re-encode any upload into a PNG that VLC's still-image module accepts.

    VLC 3's image demuxer refuses WebP and very large JPEGs (it hands them to the generic video
    demuxer, which shows one frame and ends). ffmpeg turns anything into a PNG no bigger than
    1920x1080, which also keeps the thumbnail light.
    """
    cmd = [youtube.FFMPEG or "ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
           "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {e}")
    if r.returncode != 0 or not dest.exists():
        raise HTTPException(status_code=400, detail="Couldn't read that image file.")


def play_last_if_idle(was_idle: bool) -> bool:
    if was_idle:
        items = vlc_playlist()
        if items:
            vlc("status.json", command="pl_play", id=items[-1]["id"])
            return True
    return False


def yt_fetch_and_queue(job_id: str) -> None:
    """Background: download the video, then queue the file (and play it if VLC is idle)."""
    job = yt_jobs[job_id]

    def progress(p: dict) -> None:
        job.update({k: v for k, v in p.items() if k in ("percent", "eta")})
        if p.get("meta"):
            job["title"] = job.get("prefix", "") + (p["meta"]["title"] or job["title"])
            job["thumbnail"] = p["meta"].get("thumbnail")

    try:
        meta = youtube.download(job["url"], progress, handle=job)
        path = Path(meta["path"])
        remember_extra_item(path, {
            "title": meta["title"], "name": meta.get("uploader") or "YouTube", "poster": meta.get("thumbnail"),
            "queued_by": job["user"] or None, "duration": meta.get("duration") or 0,
            "youtube": meta["url"], "options": [],
        })
        was_idle = vlc("status.json").get("state") == "stopped"
        enqueue_uri(path.as_uri())
        if job.get("group"):
            group_add(job["group"], path.as_uri())
        play_last_if_idle(was_idle)
        if job.get("group"):
            keep_group_together(job["group"])
        job.update({"status": "queued", "percent": 100.0, "title": job.get("prefix", "") + meta["title"]})
        log.info("Queued by %s: YouTube %s (%s)", job["user"] or "anonymous", meta["title"], meta["url"])
        yt_trim_cache()
        queue_event("youtube", name=job["user"], ip=job.get("ip"), device=job.get("device"), category="youtube",
                    title=meta["title"], url=meta["url"], uploader=meta.get("uploader") or None,
                    duration=meta.get("duration") or None, playlist=job.get("playlist"),
                    playlist_index=job.get("index"), playlist_count=job.get("count"), started_playback=was_idle or None)
    except HTTPException as e:
        job.update({"status": "cancelled" if job.get("cancelled") else "error", "error": e.detail})
    except Exception as e:  # keep the job table sane whatever happens
        job.update({"status": "cancelled" if job.get("cancelled") else "error", "error": str(e)[:200]})
    job.pop("proc", None)
    if job["status"] == "cancelled":
        log.info("YouTube fetch cancelled: %s", job["title"])
    if job["status"] == "error":
        log.warning("YouTube fetch failed for %s: %s", job["url"], job["error"])
        queue_event("youtube", name=job["user"], ip=job.get("ip"), device=job.get("device"), category="youtube",
                    title=job["title"], url=job["url"], playlist=job.get("playlist"), failed=True, error=job["error"])
    job["finished"] = time.time()


def yt_playlist_worker(gid: str, pl: dict, user: str, ip: str | None, device: str | None = None) -> None:
    """Download a playlist's videos one after another; each joins the queue (and the block) as it
    lands, so one playlist never holds more than one of the download slots. Cancelling the
    running download from the page stops the rest."""
    n = len(pl["entries"])
    for i, entry in enumerate(pl["entries"], 1):
        prefix = f"{pl['title']} ({i}/{n}) · "
        job_id = hashlib.sha1(f"{entry['url']}{time.time()}".encode()).hexdigest()[:10]
        with yt_lock:
            yt_jobs[job_id] = {"url": entry["url"], "title": prefix + (entry["title"] or entry["url"]), "prefix": prefix,
                               "user": user, "ip": ip, "device": device, "percent": 0.0, "eta": "", "status": "fetching",
                               "started": time.time(), "group": gid, "thumbnail": entry.get("thumbnail"),
                               "playlist": pl["title"], "index": i, "count": n}
        yt_fetch_and_queue(job_id)
        if yt_jobs.get(job_id, {}).get("status") == "cancelled":
            log.info("Playlist stopped after %d of %d: %s", i, n, pl["title"])
            break


# Finished jobs leave the pending list quickly (the real queue item has appeared); failures stay
# long enough to be read, or until dismissed.
YT_JOB_LINGER = {"queued": 3, "error": 90, "cancelled": 5}


def yt_pending() -> list[dict]:
    """Jobs still fetching, plus recently finished/failed ones so the page can show the outcome."""
    now = time.time()
    with yt_lock:
        for jid in [j for j, job in yt_jobs.items()
                    if job.get("finished") and now - job["finished"] > YT_JOB_LINGER.get(job["status"], 0)]:
            del yt_jobs[jid]
        return [
            {"id": jid, "title": job["title"], "user": job["user"], "percent": job.get("percent", 0.0),
             "eta": job.get("eta", ""), "status": job["status"], "error": job.get("error"),
             "thumbnail": job.get("thumbnail")}
            for jid, job in yt_jobs.items()
        ]


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


def filename_from_uri(uri: str) -> str:
    return Path(unquote(urlparse(uri).path)).name if uri.startswith("file:") else ""


def vlc_playlist() -> list[dict]:
    """Flatten VLC's playlist tree into the ordered list of queued items."""
    tree = vlc("playlist.json")
    nodes = tree.get("children", [])
    playlist_node = next((n for n in nodes if n.get("name") == "Playlist"), nodes[0] if nodes else {})
    items = []
    for c in playlist_node.get("children", []):
        if c.get("type") != "leaf":
            continue
        uri = c.get("uri", "")
        # VLC swaps the item name for the file's embedded title tag once it pre-parses it, which can
        # be empty or garbled. The real filename from the URI is what people recognise.
        fname = filename_from_uri(uri)
        extra = extra_lookup(uri)
        if extra:
            info = {
                "name": extra["name"], "title": extra["title"], "poster": extra.get("poster"),
                "queued_by": extra.get("queued_by"), "youtube": extra.get("youtube"), "image": extra.get("image"),
                "duration": extra.get("duration") if extra.get("duration") else c.get("duration", -1),
            }
        else:
            info = {"name": fname or c.get("name", ""), "duration": c.get("duration", -1), **library.describe(uri)}
        gid, grp = group_for_uri(uri)
        items.append({"id": int(c["id"]), "uri": uri, "current": c.get("current") == "current",
                      "group": gid, "group_label": grp["label"] if grp else None, **info})
    return items


def rebuild_upcoming(upcoming: list[dict], order: list[dict]) -> None:
    """Replace the upcoming part of VLC's playlist with `order` (VLC's HTTP API has no move)."""
    for x in upcoming:
        vlc("status.json", command="pl_delete", id=x["id"])
    for x in order:
        enqueue_uri(x["uri"])


def keep_group_together(gid: str) -> None:
    """A playlist video that just finished downloading was added at the end of VLC's playlist.
    If something else was queued while it downloaded, move it up to follow the rest of its block."""
    state = vlc("status.json").get("state")
    upcoming = upcoming_items(state, vlc_playlist())
    if len(upcoming) < 2 or upcoming[-1].get("group") != gid:
        return
    last_member = max((i for i, x in enumerate(upcoming[:-1]) if x.get("group") == gid), default=None)
    if last_member is None or last_member == len(upcoming) - 2:
        return  # no earlier member to follow, or already right behind it
    order = upcoming[:last_member + 1] + [upcoming[-1]] + upcoming[last_member + 1:-1]
    rebuild_upcoming(upcoming, order)


def upcoming_items(state: str, items: list[dict]) -> list[dict]:
    current = next((i for i, x in enumerate(items) if x["current"]), None)
    return items if state == "stopped" or current is None else items[current + 1:]


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


# The last next/previous/stop/remove and who sent it, so a skip logged a moment later can say
# who did it. Not a queue of actions: one slot, overwritten.
last_control: dict = {}


def note_control(action: str, request: Request | None) -> None:
    ip = client_ip(request)
    last_control.update({"action": action, "name": requester_name(ip, ""), "ip": ip, "device": device_kind(request),
                         "ts": time.time()})


def describe_playing(item: dict | None, status: dict) -> dict:
    """The fields worth keeping about an item for a playback event."""
    if not item:
        meta = status.get("information", {}).get("category", {}).get("meta", {})
        return {"title": meta.get("title") or meta.get("filename"), "category": "other"}
    if item.get("youtube"):
        category = "youtube"
    elif item.get("image"):
        category = "image"
    else:
        key = library.key_for_uri(item["uri"])
        category = library_category(key[0]) if key else "other"
    return {"category": category, "title": item.get("title") or item.get("name"), "episode": item.get("episode"),
            "file": item.get("name") if item.get("title") else None, "queued_by": item.get("queued_by")}


def playback_outcome(watched: float, length: float) -> tuple[str, dict] | None:
    """'finished' when it ran (almost) to the end, 'skipped' otherwise; None when VLC never
    reported a length (stills, streams), because then there is nothing to judge."""
    if not length or length <= 0:
        return None
    finished = (length - watched) <= 15 or watched / length >= 0.9
    return ("finished" if finished else "skipped",
            {"watched_s": int(watched), "length_s": int(length), "progress": round(min(watched, length) / length, 2)})


def playback_watcher() -> None:
    """Notice when the playing item changes and record whether the last one was watched to the end
    or skipped, how far in, and (if a control came in just before) by whom."""
    cur: dict | None = None  # {plid, info, time, length}
    while True:
        time.sleep(1)
        try:
            s = vlc("status.json")
            plid = s.get("currentplid", -1)
            playing = s.get("state") != "stopped" and plid not in (-1, None)
            if cur and (not playing or plid != cur["plid"]):
                outcome = playback_outcome(cur["time"], cur["length"])
                if outcome:
                    event, numbers = outcome
                    extra = {}
                    if event == "skipped" and last_control and time.time() - last_control["ts"] <= 5:
                        extra = {"via": last_control["action"], "by": last_control.get("name") or last_control.get("ip")}
                    queue_event("playback", name="", ip=None, event=event, **cur["info"], **numbers, **extra)
                cur = None
            if playing and cur is None:
                item = next((x for x in vlc_playlist() if x["id"] == plid), None)
                cur = {"plid": plid, "info": describe_playing(item, s), "time": 0.0, "length": float(s.get("length") or 0)}
            if cur and playing:
                cur["time"] = float(s.get("time") or cur["time"])
                cur["length"] = float(s.get("length") or cur["length"])
        except HTTPException:
            pass  # VLC down; try again next tick
        except Exception as e:  # never let the watcher die
            log.warning("Playback watcher error: %s", e)


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
            # Never start an item silent: if no audio track ended up selected (seen with YouTube
            # downloads), switch on the first real one.
            if not prefs.get("audio"):
                audio = tracks.get("audio", [])
                have = next((t["id"] for t in audio if t["current"]), -1)
                first = next((t["id"] for t in audio if t["id"] != -1), None)
                if have == -1 and first is not None:
                    vlc("tracks.json", command="audio_track", val=first)
                    log.info("Track watcher: audio was disabled, enabled track %s", first)
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
    if not youtube.available():
        log.warning("yt-dlp not found: YouTube disabled (pip install yt-dlp[default])")
    elif not youtube.DENO:
        log.warning("Deno not found: YouTube downloads will fail. start.ps1 fetches it into tools/deno")
    yt_trim_cache()
    image_trim_cache()
    library.scan_in_background()
    threading.Thread(target=track_watcher, name="track-watcher", daemon=True).start()
    threading.Thread(target=playback_watcher, name="playback-watcher", daemon=True).start()
    yield


app = FastAPI(title="LAN Party Projector Queue", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")  # uploaded images, for queue thumbnails


class QueueRequest(BaseModel):
    id: str
    user: str = ""


class BatchQueueRequest(BaseModel):
    ids: list[str]
    user: str = ""
    label: str = ""  # e.g. "Breaking Bad · Season 2": the items become one block in the queue


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
        "youtube_enabled": youtube.available(),
    }


@app.get("/api/youtube/search")
def youtube_search(q: str = Query("", max_length=200)):
    """Search results, or the videos of a pasted link; a playlist link also describes the playlist."""
    if youtube.is_playlist(q):
        pl = youtube.playlist(q)
        return {"results": pl["entries"],
                "playlist": {"title": pl["title"], "url": pl["url"], "count": pl["count"], "limit": youtube.PLAYLIST_MAX,
                             "mix": pl["mix"]}}
    return {"results": youtube.search(q), "playlist": None}


class YouTubeQueueRequest(BaseModel):
    url: str
    user: str = ""
    title: str = ""  # optional, shown while the download runs


@app.post("/api/queue/youtube")
def queue_youtube(req: YouTubeQueueRequest, request: Request):
    """Fetch a YouTube video (or, one at a time, a playlist) in the background and queue it when done."""
    if not youtube.is_url(req.url):
        raise HTTPException(status_code=400, detail="That doesn't look like a YouTube link.")
    if not youtube.available():
        raise HTTPException(status_code=501, detail="yt-dlp is not installed (pip install yt-dlp[default]).")
    ip = client_ip(request)
    user = requester_name(ip, req.user)
    with yt_lock:
        if sum(1 for j in yt_jobs.values() if j["status"] == "fetching") >= 3:
            raise HTTPException(status_code=429, detail="Three videos are already downloading, try again in a moment.")
    if youtube.is_playlist(req.url):
        pl = youtube.playlist(req.url)
        if not pl["entries"]:
            raise HTTPException(status_code=404, detail="That playlist is empty or private.")
        gid = new_group(pl["title"], "playlist", user or None)
        threading.Thread(target=yt_playlist_worker, args=(gid, pl, user, ip, device_kind(request)),
                         name=f"yt-pl-{gid}", daemon=True).start()
        n = len(pl["entries"])
        more = f" of {pl['count']}" if pl["count"] > n else ""
        what = "Mix" if pl["mix"] else "playlist"
        return {"message": f"Fetching {what}: {pl['title']} ({n} video{'' if n == 1 else 's'}{more}, one at a time)"}
    with yt_lock:
        job_id = hashlib.sha1(f"{req.url}{time.time()}".encode()).hexdigest()[:10]
        yt_jobs[job_id] = {"url": req.url.strip(), "title": req.title.strip() or req.url.strip(), "user": user,
                           "ip": ip, "device": device_kind(request), "percent": 0.0, "eta": "", "status": "fetching",
                           "started": time.time()}
    threading.Thread(target=yt_fetch_and_queue, args=(job_id,), name=f"yt-{job_id}", daemon=True).start()
    return {"message": f"Fetching: {yt_jobs[job_id]['title']}", "job": job_id}


@app.post("/api/queue/image")
async def queue_image(request: Request, file: UploadFile = File(...), user: str = Form(""), duration: int = Form(15)):
    """Upload a picture (meme, scoreboard, info) and show it on the projector for `duration` seconds (-1 = until skipped)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS or not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Only JPG, PNG, GIF, WebP or BMP images.")
    if duration != -1:
        duration = max(3, min(duration, 3600))
    data = bytearray()
    while chunk := await file.read(1 << 20):
        data += chunk
        if len(data) > IMAGE_MAX_MB << 20:
            raise HTTPException(status_code=413, detail=f"Image is bigger than {IMAGE_MAX_MB} MB.")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    name = requester_name(client_ip(request), user)
    # One file per upload: the same picture queued twice with different titles/durations must not collide.
    stem = f"{int(time.time() * 1000):x}_{hashlib.sha1(data).hexdigest()[:8]}"
    raw = IMAGE_DIR / f"{stem}.upload{ext}"
    raw.write_bytes(data)
    options: list[str]
    if ext == ".gif" and (gif := _ffprobe_gif(raw))[1] > 1 and gif[0] > 0.2:
        # Animated GIF: VLC plays it as a tiny video, so loop it for the requested time.
        stored = raw.rename(IMAGE_DIR / f"{stem}.gif")
        repeats = 65535 if duration == -1 else max(0, math.ceil(duration / gif[0]) - 1)
        options = [f"input-repeat={repeats}"]
    else:
        stored = IMAGE_DIR / f"{stem}.png"
        try:
            normalize_image(raw, stored)
        finally:
            raw.unlink(missing_ok=True)
        options = [f"image-duration={duration}"]
    title = Path(file.filename).stem[:80] or "Image"
    shown = "until skipped" if duration == -1 else f"{duration}s"
    remember_extra_item(stored, {
        "title": title, "name": f"Image · {shown}", "poster": f"/images/{stored.name}", "queued_by": name or None,
        "duration": 0 if duration == -1 else duration, "image": True, "options": options,
    })
    was_idle = vlc("status.json").get("state") == "stopped"
    enqueue_uri(stored.as_uri())
    log.info("Queued by %s: image %s (%s)", name or "anonymous", title, shown)
    queue_event("image", name=name, ip=client_ip(request), device=device_kind(request), category="image",
                title=title, file=stored.name, duration=None if duration == -1 else duration,
                until_skipped=duration == -1 or None, started_playback=was_idle or None)
    image_trim_cache()
    if play_last_if_idle(was_idle):
        return {"message": f"Now showing: {title}"}
    return {"message": f"Queued: {title} ({shown})"}


@app.delete("/api/youtube/jobs/{job_id}")
def dismiss_youtube_job(job_id: str):
    """Cancel a running download, or dismiss a finished/failed notice."""
    with yt_lock:
        job = yt_jobs.get(job_id)
        if not job:
            return {"message": "ok"}
        if job["status"] == "fetching":
            if youtube.cancel(job):
                return {"message": f"Cancelled: {job['title']}"}
            job["cancelled"] = True  # not started yet; the worker will see the flag
            return {"message": "Cancelling..."}
        del yt_jobs[job_id]
    return {"message": "ok"}


@app.get("/api/poster/{library_label}/{group}")
def poster(library_label: str, group: str):
    if library_label not in library.roots:
        raise HTTPException(status_code=404, detail="Unknown library.")
    file = posters.get(library_label, group)
    if not file:
        # no-store so the browser retries once the warm-up has fetched it
        raise HTTPException(status_code=404, detail="No poster.", headers={"Cache-Control": "no-store"})
    return FileResponse(file, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def enqueue(ids: list[str], user: str, *, ip: str | None = None, label: str = "",
            device: str | None = None) -> dict:
    """Add files to the end of VLC's playlist. Starts playback from the first one if VLC is idle.
    Several files with a `label` (a season, "all of" a show) become one block in the queue."""
    paths = [library.resolve(i) for i in ids]  # validate everything before touching VLC
    user = requester_name(ip, user)
    gid = new_group(label, "season", user or None) if label.strip() and len(ids) > 1 else None
    was_idle = vlc("status.json").get("state") == "stopped"
    first_new_id = None
    for item_id, path in zip(ids, paths):
        # Path.as_uri() percent-encodes spaces etc. so VLC gets a valid file:// MRL.
        vlc("status.json", command="in_enqueue", input=path.as_uri())
        if gid:
            group_add(gid, path.as_uri())
        if first_new_id is None and was_idle:
            items = vlc_playlist()
            first_new_id = items[-1]["id"] if items else None
        key = library.key_for_id(item_id)
        library.queued_by[key] = user or None
        log.info("Queued by %s: %s", user or "anonymous", item_id)
        g = library.by_relpath.get(key) or {}
        it = next((x for x in g.get("items", []) if x["id"] == item_id), {})
        queue_event("library", name=user, ip=ip, device=device, category=library_category(key[0]),
                    library=key[0], title=g.get("title") or path.stem, year=g.get("year"), file=path.name,
                    season=it.get("season"), episode=it.get("episode"), extra=it.get("extra") or None,
                    block=label.strip() or None, batch=len(ids) if len(ids) > 1 else None,
                    started_playback=(was_idle and item_id == ids[0]) or None)
    what = paths[0].name if len(paths) == 1 else f"{len(paths)} episodes"
    if was_idle and first_new_id is not None:
        vlc("status.json", command="pl_play", id=first_new_id)
        return {"message": f"Now playing: {what}"}
    return {"message": f"Queued: {what}"}


@app.post("/api/queue")
def queue_media(req: QueueRequest, request: Request):
    return enqueue([req.id], req.user, ip=client_ip(request), device=device_kind(request))


@app.post("/api/queue/batch")
def queue_batch(req: BatchQueueRequest, request: Request):
    """Queue several files in order, e.g. a whole season, as one block."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="Nothing to queue.")
    if len(req.ids) > 200:
        raise HTTPException(status_code=400, detail="That's too many at once (max 200).")
    return enqueue(req.ids, req.user, ip=client_ip(request), label=req.label, device=device_kind(request))


@app.delete("/api/queue/group/{gid}")
def remove_group(gid: str):
    """Drop every upcoming item of a season or playlist block in one go."""
    with groups_lock:
        g = queue_groups.get(gid)
    if not g:
        raise HTTPException(status_code=404, detail="That block is gone already.")
    state = vlc("status.json").get("state")
    removed = 0
    for x in upcoming_items(state, vlc_playlist()):
        if x.get("group") == gid:
            vlc("status.json", command="pl_delete", id=x["id"])
            removed += 1
    return {"message": f"Removed {removed} item{'' if removed == 1 else 's'}: {g['label']}"}


@app.post("/api/queue/reorder")
def reorder_queue(req: ReorderRequest):
    """Reorder the upcoming items. VLC's HTTP API has no 'move', so we delete them and re-add in the new order."""
    state = vlc("status.json").get("state")
    upcoming = upcoming_items(state, vlc_playlist())
    by_id = {x["id"]: x for x in upcoming}
    if sorted(req.ids) != sorted(by_id):
        raise HTTPException(status_code=409, detail="The queue changed, refresh and try again.")
    if [x["id"] for x in upcoming] != req.ids:
        rebuild_upcoming(upcoming, [by_id[i] for i in req.ids])
    return {"message": "Queue reordered", "queue": vlc_playlist()}


@app.get("/api/status")
def status():
    """What VLC is doing right now plus the full queue."""
    s = vlc("status.json")
    meta = s.get("information", {}).get("category", {}).get("meta", {})
    queue = vlc_playlist()
    prune_groups(queue)
    current = next((x for x in queue if x["current"]), None)
    if current and (current.get("youtube") or current.get("image")):
        now_playing = current["name"]  # uploader / "Image · 15s"; VLC's own meta is just the cache filename
    else:
        now_playing = meta.get("title") or meta.get("filename") or (current or {}).get("name")
    title = current["title"] if current else None
    if title and current.get("episode"):
        title = f"{title} · {current['episode']}"
    return {
        "state": s.get("state"),
        "now_playing": now_playing,
        "title": title,
        "poster": current["poster"] if current else None,
        "time": s.get("time", 0),
        "length": s.get("length", 0),
        "volume": s.get("volume", 0),
        "fullscreen": bool(s.get("fullscreen")),
        "queue": queue,
        "pending": yt_pending(),
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
def control(req: ControlRequest, request: Request):
    cmd = CONTROL_COMMANDS.get(req.action)
    if not cmd:
        raise HTTPException(status_code=400, detail=f"Unknown action '{req.action}'.")
    if req.action in ("next", "previous", "stop"):
        note_control(req.action, request)  # the playback watcher credits a skip to this
        queue_event("control", name=last_control["name"], ip=last_control["ip"], device=last_control["device"],
                    action=req.action)
    vlc("status.json", command=cmd)
    return {"message": "ok"}


@app.delete("/api/queue/{item_id}")
def remove_from_queue(item_id: int, request: Request):
    s = vlc("status.json")
    item = next((x for x in vlc_playlist() if x["id"] == item_id), None)
    if item and s.get("currentplid") == item_id and s.get("state") != "stopped":
        note_control("remove", request)  # removing the playing item is a skip
    ip = client_ip(request)
    queue_event("remove", name=requester_name(ip, ""), ip=ip, device=device_kind(request),
                **(describe_playing(item, s) if item else {}), was_playing=bool(item and s.get("currentplid") == item_id) or None)
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
