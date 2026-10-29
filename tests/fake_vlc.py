"""A stand-in for VLC's HTTP interface, close enough for the feeder.

Like VLC: playlist ids only ever grow; playback moves down the list and stops after
the last item; deleting the playing item stops playback; pl_next past the end
stops. Files whose path contains "youtube" start with audio disabled (as VLC did
with the downloads), others on their first track. ``play_for(seconds)`` moves time
on, ending items as their length runs out.
"""

from urllib.parse import unquote, urlparse


class FakeVlc:
    def __init__(self, lengths: dict[str, float] | None = None, default_length: float = 100.0):
        self.items: list[dict] = []  # {id, uri, options}
        self.next_id = 3
        self.current: int | None = None  # VLC id
        self.state = "stopped"
        self.time = 0.0
        self.lengths = lengths or {}
        self.default_length = default_length
        self.audio: dict[int, int] = {}  # VLC id -> selected audio track id
        self.modes = {"random": False, "repeat": False, "loop": False}  # toggled by pl_<mode>
        self.calls: list[tuple] = []

    # --- helpers for tests ------------------------------------------------------------------
    def name(self, uri: str) -> str:
        return unquote(urlparse(uri).path).rsplit("/", 1)[-1]

    def length_of(self, plid: int) -> float:
        item = self._item(plid)
        return self.lengths.get(self.name(item["uri"]), self.default_length)

    def playing_name(self) -> str | None:
        if self.state == "stopped" or self.current is None:
            return None
        return self.name(self._item(self.current)["uri"])

    def names(self) -> list[str]:
        return [self.name(i["uri"]) for i in self.items]

    def play_for(self, seconds: float) -> None:
        if self.state != "playing":
            return
        self.time += seconds
        while self.state == "playing" and self.time >= self.length_of(self.current):
            over = self.time - self.length_of(self.current)
            self._move_on()
            self.time = over if self.state == "playing" else 0.0

    def add_foreign(self, uri: str) -> int:
        """Someone drags a file into VLC at the projector."""
        return self._append(uri, None)

    # --- the HTTP interface ------------------------------------------------------------------
    def __call__(self, endpoint: str, **params) -> dict:
        self.calls.append((endpoint, dict(params)))
        if endpoint == "playlist.json":
            return self._tree()
        if endpoint == "tracks.json":
            return self._tracks(params)
        if endpoint != "status.json":
            raise ValueError(endpoint)
        cmd = params.get("command")
        if cmd == "in_enqueue":
            self._append(params["input"], params.get("option"))
        elif cmd == "pl_play":
            self._start(int(params["id"]))
        elif cmd == "pl_delete":
            plid = int(params["id"])
            if plid == self.current:
                self.state, self.current, self.time = "stopped", None, 0.0
            self.items = [i for i in self.items if i["id"] != plid]
        elif cmd == "pl_next":
            if self.current is not None:
                self._move_on()
        elif cmd == "pl_stop":
            self.state, self.time = "stopped", 0.0
        elif cmd == "pl_pause":
            if self.state == "playing":
                self.state = "paused"
            elif self.state == "paused":
                self.state = "playing"
        elif cmd in ("pl_random", "pl_repeat", "pl_loop"):
            mode = cmd[3:]
            self.modes[mode] = not self.modes[mode]
        elif cmd is not None:
            raise ValueError(cmd)
        playing = self.state != "stopped" and self.current is not None
        return {
            "state": self.state,
            "currentplid": self.current if playing else -1,
            "time": int(self.time),
            "length": int(self.length_of(self.current)) if playing else 0,
            **self.modes,
        }

    # --- internals ------------------------------------------------------------------------------
    def _item(self, plid: int) -> dict:
        return next(i for i in self.items if i["id"] == plid)

    def _append(self, uri: str, options) -> int:
        plid = self.next_id
        self.next_id += 1
        self.items.append({"id": plid, "uri": uri, "options": options})
        self.audio[plid] = -1 if "youtube" in uri else 1
        return plid

    def _start(self, plid: int) -> None:
        self._item(plid)
        self.current, self.state, self.time = plid, "playing", 0.0

    def _move_on(self) -> None:
        ids = [i["id"] for i in self.items]
        at = ids.index(self.current)
        if at + 1 < len(ids):
            self._start(ids[at + 1])
        else:
            self.state, self.time = "stopped", 0.0

    def _tree(self) -> dict:
        leaves = [
            {
                "type": "leaf",
                "id": str(i["id"]),
                "uri": i["uri"],
                "name": self.name(i["uri"]),
                **({"current": "current"} if i["id"] == self.current else {}),
            }
            for i in self.items
        ]
        return {"children": [{"name": "Playlist", "children": leaves}, {"name": "Media Library", "children": []}]}

    def _tracks(self, params: dict) -> dict:
        if params.get("command") == "audio_track" and self.current is not None:
            self.audio[self.current] = int(params["val"])
        if self.current is None or self.state == "stopped":
            return {"audio": [], "subtitle": []}
        chosen = self.audio.get(self.current, 1)
        audio = [{"id": t, "name": f"Track {t}", "current": t == chosen} for t in (-1, 1)]
        return {"audio": audio, "subtitle": []}
