"""The feeder: the one piece of code that changes VLC's playlist.

The app owns the queue (playqueue.py). VLC only ever holds what is playing and the
next entry; the feeder keeps it that way, follows what VLC does (an item ending, next
pressed at the projector), starts the next entry when VLC is idle and the entry is
downloaded, and puts YouTube items on their first audio track. Page controls (next,
previous, stop, play, pause) come through here too, so nothing else touches VLC's
playlist and no two changes interleave. Spec: docs/queue_rebuild.md.

``vlc(endpoint, **params)`` is the app's VLC call (requests/status.json,
playlist.json, tracks.json); tests pass a fake. Items in VLC the feeder didn't add
(dragged in at the projector) are left alone.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from playqueue import Entry, PlayQueue

log = logging.getLogger("feeder")

# An item that stops more than this far from its end was stopped, not finished.
END_SLACK_S = 5.0
# How many ticks to wait for an item's audio tracks to appear before giving up.
TRACK_TICKS = 15


def entry_uri(entry: Entry) -> str:
    return Path(entry.path).resolve().as_uri()


def flatten_playlist(tree: dict) -> list[dict]:
    """VLC's playlist.json tree -> [{id, uri, current}] for the main playlist."""
    nodes = tree.get("children", [])
    main = next((n for n in nodes if n.get("name") == "Playlist"), nodes[0] if nodes else {})
    return [
        {"id": int(c["id"]), "uri": c.get("uri", ""), "current": c.get("current") == "current"}
        for c in main.get("children", [])
        if c.get("type") == "leaf"
    ]


class Feeder:
    def __init__(
        self,
        queue: PlayQueue,
        vlc: Callable[..., dict],
        on_start: Callable[[Entry], None] | None = None,
        on_end: Callable[[Entry, float, float], None] | None = None,
    ):
        self.queue = queue
        self.vlc = vlc
        self.on_start = on_start  # an entry started playing
        self.on_end = on_end  # an entry stopped playing: (entry, seconds played, length)
        self._lock = threading.RLock()
        self.owned: dict[int, str] = {}  # VLC id -> entry id, for items the feeder added
        self.autoplay = True  # False after stop, until play (or anything is queued and play pressed)
        self._seen: tuple[float, float] = (0.0, 0.0)  # current item's last (time, length)
        self._track_entry: str | None = None  # YouTube entry still waiting for its audio track
        self._track_ticks = 0
        self._stop = threading.Event()

    # --- the loop -------------------------------------------------------------------------
    def start(self, interval: float = 1.0) -> threading.Thread:
        def run():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception as e:  # VLC down, or a bug: never let the feeder die
                    log.debug("feeder tick failed: %s", e)

        t = threading.Thread(target=run, name="feeder", daemon=True)
        t.start()
        return t

    def stop_loop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        with self._lock:
            status = self.vlc("status.json")
            items = flatten_playlist(self.vlc("playlist.json"))
            state = status.get("state", "stopped")
            plid = status.get("currentplid", -1)
            playing = state in ("playing", "paused") and plid not in (-1, None)

            if playing:
                self._follow(int(plid), status)
            elif self.queue.current is not None:
                self._ended_or_stopped()

            items = self._tidy(items)
            self._preload(items)
            if not playing and self.autoplay:
                self._play_next()
            self._audio_track()

    # --- following VLC ----------------------------------------------------------------------
    def _follow(self, plid: int, status: dict) -> None:
        entry_id = self.owned.get(plid)
        current = self.queue.current
        if entry_id and current and current.id == entry_id:
            self._seen = (float(status.get("time") or 0), float(status.get("length") or 0))
            return
        if current is not None:  # VLC moved on from it (ended, or next at the projector)
            self._end(current)
        if entry_id:
            started = self.queue.begin(entry_id)
            if started:
                self._started(started)
        else:
            # Something the feeder didn't add is playing; the queue resumes after it.
            if self.queue.current is not None:
                self.queue.stopped()
        self._seen = (float(status.get("time") or 0), float(status.get("length") or 0))

    def _ended_or_stopped(self) -> None:
        current = self.queue.current
        watched, length = self._seen
        ended = length <= 0 or (length - watched) <= END_SLACK_S
        self._end(current)
        if ended:
            self.queue.stopped()  # finished with nothing ready after it: wait for the next
        else:
            self.queue.requeue_current()  # stopped at the projector: hold the queue
            self.autoplay = False

    def _end(self, entry: Entry) -> None:
        if self.on_end:
            self.on_end(entry, *self._seen)
        self._seen = (0.0, 0.0)

    def _started(self, entry: Entry) -> None:
        self._seen = (0.0, 0.0)
        if entry.kind == "youtube":
            self._track_entry, self._track_ticks = entry.id, 0
        if self.on_start:
            self.on_start(entry)

    # --- keeping VLC's window ----------------------------------------------------------------
    def _tidy(self, items: list[dict]) -> list[dict]:
        """Delete the feeder's items that are neither playing nor the next entry."""
        current = self.queue.current
        nxt = self.queue.peek()
        keep = {e.id for e in (current, nxt) if e is not None}
        playing_plid = next((i["id"] for i in items if i["current"]), None)
        left = []
        for item in items:
            entry_id = self.owned.get(item["id"])
            if entry_id is not None and entry_id not in keep and item["id"] != playing_plid:
                self.vlc("status.json", command="pl_delete", id=item["id"])
                del self.owned[item["id"]]
                continue
            left.append(item)
        # forget ids VLC no longer has
        present = {i["id"] for i in left}
        for plid in [p for p in self.owned if p not in present]:
            del self.owned[plid]
        return left

    def _vlc_id_for(self, entry: Entry) -> int | None:
        return next((plid for plid, eid in self.owned.items() if eid == entry.id), None)

    def _enqueue(self, entry: Entry) -> int | None:
        before = {i["id"] for i in flatten_playlist(self.vlc("playlist.json"))}
        params = {"command": "in_enqueue", "input": entry_uri(entry)}
        if entry.meta.get("options"):
            params["option"] = entry.meta["options"]
        self.vlc("status.json", **params)
        after = flatten_playlist(self.vlc("playlist.json"))
        new = [i["id"] for i in after if i["id"] not in before]
        if not new:
            log.warning("feeder: VLC didn't take %s", entry.path)
            return None
        self.owned[new[-1]] = entry.id
        return new[-1]

    def _preload(self, items: list[dict]) -> None:
        """The next entry sits right after what is playing, so VLC moves on to it by
        itself. One that ended up before the playing item (after ⏮) is re-added."""
        nxt = self.queue.peek()
        if nxt is None:
            return
        plid = self._vlc_id_for(nxt)
        if plid is not None:
            ids = [i["id"] for i in items]
            playing = next((i["id"] for i in items if i["current"]), None)
            if playing in ids and plid in ids and ids.index(plid) < ids.index(playing):
                self.vlc("status.json", command="pl_delete", id=plid)
                del self.owned[plid]
                plid = None
        if plid is None:
            self._enqueue(nxt)

    def _play_next(self) -> None:
        nxt = self.queue.peek()
        if nxt is None:
            return
        plid = self._vlc_id_for(nxt) or self._enqueue(nxt)
        if plid is None:
            return
        self.vlc("status.json", command="pl_play", id=plid)
        started = self.queue.begin(nxt.id)
        if started:
            self._started(started)

    def _audio_track(self) -> None:
        """YouTube items play their first audio track; VLC sometimes starts them with
        audio disabled. Retried for a few ticks while the tracks appear."""
        current = self.queue.current
        if self._track_entry is None or current is None or current.id != self._track_entry:
            self._track_entry = None
            return
        tracks = self.vlc("tracks.json").get("audio", [])
        real = [t for t in tracks if t.get("id") != -1]
        if not real:
            self._track_ticks += 1
            if self._track_ticks >= TRACK_TICKS:
                self._track_entry = None
            return
        first = real[0]
        if not first.get("current"):
            self.vlc("tracks.json", command="audio_track", val=first["id"])
        self._track_entry = None

    # --- page controls ------------------------------------------------------------------------
    def next(self) -> None:
        """Skip what is playing. The next entry plays now if it is ready; if not, the
        queue waits for it."""
        with self._lock:
            current = self.queue.current
            nxt = self.queue.peek()
            if nxt is not None and self._vlc_id_for(nxt) is not None:
                self.vlc("status.json", command="pl_next")
                self.autoplay = True
                return
            if current is not None:
                self._end(current)
                self.queue.stopped()
            self.vlc("status.json", command="pl_stop")
            self.autoplay = True
            self._play_next()

    def previous(self) -> None:
        """Play the most recently finished entry again (it comes back as a solo entry)."""
        with self._lock:
            current = self.queue.current
            if current is not None:
                self._end(current)
            again = self.queue.previous()
            if again is None:
                return
            self.autoplay = True
            plid = self._enqueue(again)
            if plid is None:
                return
            self.vlc("status.json", command="pl_play", id=plid)
            started = self.queue.begin(again.id)
            if started:
                self._started(started)

    def stop(self) -> None:
        """Stop and hold: what was playing goes back to the front, nothing starts
        until play."""
        with self._lock:
            current = self.queue.current
            if current is not None:
                self._end(current)
                self.queue.requeue_current()
            self.autoplay = False
            self.vlc("status.json", command="pl_stop")

    def play(self) -> None:
        """Resume a pause, or start the queue after a stop."""
        with self._lock:
            status = self.vlc("status.json")
            self.autoplay = True
            if status.get("state") == "paused":
                self.vlc("status.json", command="pl_pause")
            elif status.get("state") != "playing":
                self._play_next()

    def pause(self) -> None:
        with self._lock:
            self.vlc("status.json", command="pl_pause")

    def waiting(self) -> bool:
        """Idle only because the next entry is still downloading (the page says so)."""
        with self._lock:
            return (
                self.autoplay
                and self.queue.current is None
                and bool(self.queue.upcoming)
                and self.queue.peek() is None
            )
