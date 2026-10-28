"""The queue the app owns: entries, blocks, ordering rules, history, saving.

Pure logic: no VLC calls and no network. The feeder (the one thread that talks to
VLC's playlist) asks this module what plays next and tells it when something
started; the downloader asks what to fetch next and reports back; the routes edit
it. Spec: docs/queue_rebuild.md.

Rules kept here, so every caller gets them for free:
- an entry's identity is its own id; membership in a block and credit come from the
  entry, never from a file path or a VLC id
- a block's entries are always next to each other in the queue; any reorder that
  would split one is normalized back
- a block disappears once nothing of it is queued or playing, and is never revived
- the next entry only plays once it is ready (downloaded); a failed entry leaves
  the queue
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

KINDS = ("file", "youtube", "image")
STATUSES = ("ready", "pending", "downloading", "failed")
BLOCK_KINDS = ("playlist", "mix", "season")
HISTORY_SIZE = 20
FAILED_SHOWN = 10
SAVE_VERSION = 1


def new_id() -> str:
    return secrets.token_hex(6)


@dataclass
class Entry:
    """One thing in the queue. ``path`` is the local file once there is one;
    ``url``/``video_id`` say where a YouTube entry comes from."""

    id: str
    kind: str
    title: str
    queued_by: str | None = None
    added: float = 0.0
    block: str | None = None
    status: str = "ready"
    path: str | None = None
    url: str | None = None
    video_id: str | None = None
    cap: str | None = None  # why the download is capped at 720p: "playlist" / "music"
    error: str | None = None
    meta: dict = field(default_factory=dict)  # poster, duration, uploader name, ...

    @property
    def ready(self) -> bool:
        return self.status == "ready" and bool(self.path)


@dataclass
class Block:
    id: str
    label: str
    kind: str
    queued_by: str | None = None
    created: float = 0.0
    source_url: str | None = None
    size: int = 0  # how many entries it started with


class PlayQueue:
    """Thread-safe; every public method takes the lock and saves after a change."""

    def __init__(
        self,
        path: Path | None = None,
        history_size: int = HISTORY_SIZE,
        clock=time.time,
        make_id=new_id,
    ):
        self.path = Path(path) if path else None
        self._clock = clock
        self._make_id = make_id
        self._lock = threading.RLock()
        self.current: Entry | None = None
        self.upcoming: list[Entry] = []
        self.history: deque[Entry] = deque(maxlen=history_size)
        self.blocks: dict[str, Block] = {}
        self.failed: deque[Entry] = deque(maxlen=FAILED_SHOWN)
        if self.path and self.path.exists():
            self._load()

    # --- adding ---------------------------------------------------------------------------
    def _entry(self, kind: str, title: str, **fields) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        status = fields.pop("status", None)
        if status is None:
            status = "ready" if fields.get("path") else "pending"
        return Entry(id=self._make_id(), kind=kind, title=title, added=self._clock(), status=status, **fields)

    def add(self, kind: str, title: str, **fields) -> Entry:
        """A solo entry at the end of the queue."""
        with self._lock:
            fields.pop("block", None)  # solo entries never belong to a block
            entry = self._entry(kind, title, **fields)
            self.upcoming.append(entry)
            self._save()
            return entry

    def add_block(
        self,
        label: str,
        kind: str,
        items: list[dict],
        queued_by: str | None = None,
        source_url: str | None = None,
    ) -> tuple[Block, list[Entry]]:
        """A new block at the end of the queue. ``items`` are the entry fields
        (``kind`` and ``title`` required). Always a new block: nothing is matched
        to an earlier one, even for the same ``source_url``."""
        if kind not in BLOCK_KINDS:
            raise ValueError(f"unknown block kind {kind!r}")
        if not items:
            raise ValueError("a block needs at least one item")
        with self._lock:
            block = Block(
                id=self._make_id(),
                label=label,
                kind=kind,
                queued_by=queued_by,
                created=self._clock(),
                source_url=source_url,
                size=len(items),
            )
            entries = []
            for item in items:
                item = dict(item)
                item.setdefault("queued_by", queued_by)
                item["block"] = block.id
                entries.append(self._entry(item.pop("kind"), item.pop("title"), **item))
            self.blocks[block.id] = block
            self.upcoming.extend(entries)
            self._save()
            return block, entries

    # --- editing --------------------------------------------------------------------------
    def get(self, entry_id: str) -> Entry | None:
        with self._lock:
            if self.current and self.current.id == entry_id:
                return self.current
            return next((e for e in self.upcoming if e.id == entry_id), None)

    def remove(self, entry_id: str) -> Entry | None:
        """Take an upcoming entry out of the queue. (Removing what is playing is a
        skip, which the feeder does.)"""
        with self._lock:
            entry = next((e for e in self.upcoming if e.id == entry_id), None)
            if entry is None:
                return None
            self.upcoming.remove(entry)
            self._drop_empty_blocks()
            self._save()
            return entry

    def remove_block(self, block_id: str) -> list[Entry]:
        """Take every upcoming entry of a block out. Final: the block is gone once
        nothing of it is playing, and re-queueing makes a new one."""
        with self._lock:
            gone = [e for e in self.upcoming if e.block == block_id]
            self.upcoming = [e for e in self.upcoming if e.block != block_id]
            self._drop_empty_blocks()
            self._save()
            return gone

    def reorder(self, ids: list[str]) -> bool:
        """Put the upcoming entries in this order. Unknown ids are ignored; entries
        missing from ``ids`` keep their relative order after the named ones (the page
        may be a moment behind). A block is kept together where its first entry
        lands, its entries in the order given. Returns whether anything moved."""
        with self._lock:
            by_id = {e.id: e for e in self.upcoming}
            named = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
            seen = {e.id for e in named}
            wanted = named + [e for e in self.upcoming if e.id not in seen]
            ordered = normalize(wanted, front=self.current.block if self.current else None)
            changed = [e.id for e in ordered] != [e.id for e in self.upcoming]
            if changed:
                self.upcoming = ordered
                self._save()
            return changed

    # --- downloads --------------------------------------------------------------------------
    def to_download(self, slots: int) -> list[Entry]:
        """The next entries to fetch, in queue order, filling up to ``slots``
        downloads at once (counting the ones already running)."""
        with self._lock:
            running = sum(1 for e in self.upcoming if e.status == "downloading")
            free = max(0, slots - running)
            return [e for e in self.upcoming if e.status == "pending"][:free]

    def set_status(self, entry_id: str, status: str, path: str | None = None, error: str | None = None, **meta) -> Entry | None:
        """The downloader's report. ``ready`` needs the file's path; ``failed``
        takes the entry out of the queue (shown in ``failed`` for a while) and its
        block carries on without it."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            entry = next((e for e in self.upcoming if e.id == entry_id), None)
            if entry is None:
                return None  # removed while downloading
            if status == "ready" and not path:
                raise ValueError("a ready entry needs its file path")
            entry.status, entry.error = status, error
            if path:
                entry.path = path
            entry.meta.update(meta)
            if status == "failed":
                self.upcoming.remove(entry)
                self.failed.append(entry)
                self._drop_empty_blocks()
            self._save()
            return entry

    def in_use(self) -> set[str]:
        """Files the queue still needs (the cache must not trim these)."""
        with self._lock:
            entries = self.upcoming + ([self.current] if self.current else [])
            return {e.path for e in entries if e.path}

    # --- playback ---------------------------------------------------------------------------
    def peek(self) -> Entry | None:
        """What plays next, if it is ready. None while the next entry is still
        downloading: playback waits rather than skipping ahead."""
        with self._lock:
            if self.upcoming and self.upcoming[0].ready:
                return self.upcoming[0]
            return None

    def advance(self) -> Entry | None:
        """What was playing goes to history; the next entry becomes current if it is
        ready, else nothing is playing until it is."""
        with self._lock:
            if self.current is not None:
                self.history.append(self.current)
                self.current = None
            if self.upcoming and self.upcoming[0].ready:
                self.current = self.upcoming.pop(0)
            self._drop_empty_blocks()
            self._save()
            return self.current

    def begin(self, entry_id: str) -> Entry | None:
        """VLC started this entry: what was playing goes to history and this entry
        becomes current, wherever it is in the queue (VLC can move on to the item
        the feeder preloaded a moment before a reorder changed the front)."""
        with self._lock:
            entry = next((e for e in self.upcoming if e.id == entry_id), None)
            if entry is None:
                return None
            if self.current is not None:
                self.history.append(self.current)
            self.upcoming.remove(entry)
            self.current = entry
            self.upcoming = normalize(self.upcoming, front=entry.block)
            self._drop_empty_blocks()
            self._save()
            return entry

    def requeue_current(self) -> Entry | None:
        """Stop: what was playing goes back to the front, so play starts it again."""
        with self._lock:
            entry, self.current = self.current, None
            if entry is not None:
                self.upcoming.insert(0, entry)
                self._save()
            return entry

    def stopped(self) -> None:
        """Playback stopped with nothing after it: what was playing goes to history."""
        with self._lock:
            if self.current is not None:
                self.history.append(self.current)
                self.current = None
                self._drop_empty_blocks()
                self._save()

    def previous(self) -> Entry | None:
        """⏮: the most recently played entry comes back to the front (as a solo
        entry with a new id) ahead of whatever was playing, which goes back to the
        front too. The caller then advances to play it."""
        with self._lock:
            if not self.history:
                return None
            if self.current is not None:
                self.upcoming.insert(0, self.current)
                self.current = None
            last = self.history.pop()
            again = replace(last, id=self._make_id(), block=None, added=self._clock(), meta=dict(last.meta))
            self.upcoming.insert(0, again)
            self._save()
            return again

    # --- cutover ----------------------------------------------------------------------------
    def adopt(self, items: list[dict]) -> list[Entry]:
        """First start of the new build: what VLC was holding becomes solo entries,
        in order. Only when the queue is empty, so it can run on every start."""
        with self._lock:
            if self.upcoming or self.current or self.history:
                return []
            entries = []
            for item in items:
                item = dict(item)
                item.pop("block", None)
                entries.append(self._entry(item.pop("kind", "file"), item.pop("title"), **item))
            self.upcoming.extend(entries)
            self._save()
            return entries

    # --- what the page shows ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            def row(e: Entry) -> dict:
                d = asdict(e)
                b = self.blocks.get(e.block) if e.block else None
                d["block_label"] = b.label if b else None
                d["block_kind"] = b.kind if b else None
                return d

            return {
                "current": row(self.current) if self.current else None,
                "upcoming": [row(e) for e in self.upcoming],
                "blocks": {bid: asdict(b) for bid, b in self.blocks.items()},
                "failed": [asdict(e) for e in self.failed],
                "history": [asdict(e) for e in reversed(self.history)],
            }

    # --- internals ----------------------------------------------------------------------------
    def _drop_empty_blocks(self) -> None:
        live = {e.block for e in self.upcoming if e.block}
        if self.current and self.current.block:
            live.add(self.current.block)
        for bid in list(self.blocks):
            if bid not in live:
                del self.blocks[bid]

    def _save(self) -> None:
        if not self.path:
            return
        data = {
            "version": SAVE_VERSION,
            "current": asdict(self.current) if self.current else None,
            "upcoming": [asdict(e) for e in self.upcoming],
            "history": [asdict(e) for e in self.history],
            "blocks": [asdict(b) for b in self.blocks.values()],
            "failed": [asdict(e) for e in self.failed],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        current = data.get("current")
        self.current = Entry(**current) if current else None
        self.upcoming = [Entry(**e) for e in data.get("upcoming", [])]
        for e in self.upcoming:
            if e.status == "downloading":
                e.status = "pending"  # the download was cut off by the restart: resume it
        self.history.extend(Entry(**e) for e in data.get("history", []))
        self.failed.extend(Entry(**e) for e in data.get("failed", []))
        self.blocks = {b["id"]: Block(**b) for b in data.get("blocks", [])}
        self.upcoming = normalize(self.upcoming, front=self.current.block if self.current else None)
        self._drop_empty_blocks()


def normalize(entries: list[Entry], front: str | None = None) -> list[Entry]:
    """Keep every block together: each block lands where its first entry is, with
    its entries in their given order; solo entries keep their places around it.
    ``front`` is the block that is playing: its remaining entries stay first, so
    nothing can be dropped into the middle of a block in progress."""
    members: dict[str, list[Entry]] = {}
    for e in entries:
        if e.block:
            members.setdefault(e.block, []).append(e)
    out: list[Entry] = []
    placed: set[str] = set()
    if front in members:
        out.extend(members[front])
        placed.add(front)
    for e in entries:
        if not e.block:
            out.append(e)
        elif e.block not in placed:
            placed.add(e.block)
            out.extend(members[e.block])
    return out
