"""The download worker: queue order, two at a time, the cache, shared videos,
failures, removals; and youtube.download() itself against a fake yt-dlp."""

import itertools
import sys
from pathlib import Path

import pytest
from fake_vlc import FakeVlc

import youtube
from feeder import Feeder
from playqueue import PlayQueue
from youtube import Downloader

FAKE_YTDLP = str(Path(__file__).with_name("fake_ytdlp.py"))


def vid(n) -> str:
    return f"vid{n:08d}"  # 11 characters, like a real id


def url(v) -> str:
    return f"https://www.youtube.com/watch?v={v}"


class FakeFetch:
    """Downloads that finish when the test says so."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.started: list[str] = []  # urls, in the order fetches began
        self.waiting: list = []  # spawned runs not yet finished
        self.killed: list[dict] = []
        self.fail: dict[str, str] = {}  # url -> error

    def fetch(self, u, on_progress, handle=None, cap=None):
        self.started.append(u)
        on_progress({"percent": 40.0, "eta": "00:05"})
        if u in self.fail:
            raise RuntimeError(self.fail[u])
        v = youtube.video_id_of(u)
        path = self.tmp_path / f"{v}.mkv"
        path.write_bytes(b"x")
        return {"path": str(path), "duration": 200, "quality": f"720p ({cap})" if cap else "1080p"}

    def kill(self, handle):
        self.killed.append(handle)
        return True

    def spawn(self, fn):
        self.waiting.append(fn)

    def finish(self, n=1):
        """Let the oldest ``n`` running downloads finish (each started its fetch already)."""
        for _ in range(n):
            self.waiting.pop(0)()


def setup(tmp_path, cached=None, slots=2):
    ids = (f"e{n}" for n in itertools.count(1))
    q = PlayQueue(make_id=lambda: next(ids))
    ff = FakeFetch(tmp_path)
    finished = []
    trimmed = []
    d = Downloader(
        q,
        fetch=ff.fetch,
        kill=ff.kill,
        cached=lambda v: (cached or {}).get(v),
        trim=trimmed.append,
        slots=slots,
        on_finish=lambda entries, meta, error: finished.append(([e.title for e in entries], error)),
        spawn=ff.spawn,
    )
    return q, d, ff, finished, trimmed


def add_songs(q, n, block=False):
    items = [{"kind": "youtube", "title": f"S{i}", "url": url(vid(i)), "video_id": vid(i)} for i in range(1, n + 1)]
    if block:
        return q.add_block("Mix - Test", "mix", [dict(i, cap="playlist") for i in items])[1]
    return [q.add(**i) for i in items]


def statuses(q):
    return [(e.title, e.status) for e in q.upcoming]


def test_two_at_a_time_in_queue_order(tmp_path):
    q, d, ff, _, _ = setup(tmp_path)
    add_songs(q, 3)
    d.tick()
    assert statuses(q) == [("S1", "downloading"), ("S2", "downloading"), ("S3", "pending")]
    ff.finish()  # S1 done, its slot frees up
    d.tick()
    assert statuses(q) == [("S1", "ready"), ("S2", "downloading"), ("S3", "downloading")]
    ff.finish(2)
    assert all(e.ready for e in q.upcoming)
    assert ff.started == [url(vid(1)), url(vid(2)), url(vid(3))]


def test_dragging_an_entry_up_moves_its_download_up(tmp_path):
    q, d, ff, _, _ = setup(tmp_path, slots=1)
    s1, s2, s3 = add_songs(q, 3)
    d.tick()
    q.reorder([s1.id, s3.id, s2.id])
    ff.finish()
    d.tick()
    assert statuses(q) == [("S1", "ready"), ("S3", "downloading"), ("S2", "pending")]


def test_a_cached_video_is_ready_without_yt_dlp(tmp_path):
    q, d, _, _, _ = setup(tmp_path, cached={vid(2): "cache/youtube/vid2.mkv"})
    add_songs(q, 2)
    d.tick()
    assert q.upcoming[1].ready and q.upcoming[1].path == "cache/youtube/vid2.mkv"
    assert list(d.jobs) == [vid(1)]  # only S1 goes to yt-dlp


def test_cached_entries_further_down_are_ready_even_with_both_slots_busy(tmp_path):
    q, d, _, _, _ = setup(tmp_path, cached={vid(3): "c3.mkv"})
    add_songs(q, 3)
    d.tick()
    assert statuses(q) == [("S1", "downloading"), ("S2", "downloading"), ("S3", "ready")]


def test_the_same_video_queued_twice_downloads_once(tmp_path):
    q, d, ff, finished, _ = setup(tmp_path)
    solo = q.add("youtube", "Solo", url=url(vid(1)), video_id=vid(1))
    _, (in_mix,) = q.add_block("Mix", "mix", [{"kind": "youtube", "title": "In Mix", "url": url(vid(1)), "video_id": vid(1)}])
    d.tick()
    assert solo.status == in_mix.status == "downloading" and len(d.jobs) == 1
    ff.finish()
    assert solo.ready and in_mix.ready and solo.path == in_mix.path and solo.id != in_mix.id
    assert ff.started == [url(vid(1))]
    assert sorted(finished[0][0]) == ["In Mix", "Solo"]


def test_a_failed_song_drops_out_and_the_mix_carries_on(tmp_path):
    q, d, ff, finished, _ = setup(tmp_path)
    add_songs(q, 3, block=True)
    ff.fail[url(vid(2))] = "Video unavailable"
    d.tick()
    ff.finish(2)
    d.tick()
    ff.finish()
    assert [e.title for e in q.upcoming] == ["S1", "S3"] and all(e.ready for e in q.upcoming)
    assert [e.title for e in q.failed] == ["S2"] and q.failed[0].error == "Video unavailable"
    assert (["S2"], "Video unavailable") in finished


def test_removing_an_entry_stops_its_download(tmp_path):
    q, d, ff, finished, _ = setup(tmp_path)
    s1, _, _ = add_songs(q, 3)
    d.tick()
    q.remove(s1.id)
    d.tick()  # S1's download is killed and S3 takes its slot
    assert len(ff.killed) == 1 and [e.title for e in q.upcoming if e.status == "downloading"] == ["S2", "S3"]
    ff.finish()  # the killed fetch returns anyway: nothing comes of it
    assert q.get(s1.id) is None and not [f for f in finished if "S1" in f[0]]


def test_a_shared_download_survives_removing_one_of_its_entries(tmp_path):
    q, d, ff, _, _ = setup(tmp_path)
    a = q.add("youtube", "A", url=url(vid(1)), video_id=vid(1))
    b = q.add("youtube", "B", url=url(vid(1)), video_id=vid(1))
    d.tick()
    q.remove(a.id)
    d.tick()
    assert not ff.killed
    ff.finish()
    assert b.ready


def test_block_songs_are_capped(tmp_path):
    q, d, ff, _, _ = setup(tmp_path)
    add_songs(q, 1, block=True)
    d.tick()
    ff.finish()
    assert q.upcoming[0].meta["quality"] == "720p (playlist)"


def test_progress_while_downloading_then_the_cache_is_trimmed(tmp_path):
    q, d, ff, _, trimmed = setup(tmp_path)
    (s1,) = add_songs(q, 1)
    seen = []

    def fetch(u, on_progress, **kw):
        on_progress({"percent": 40.0, "eta": "00:05"})
        seen.append(d.progress())
        return ff.fetch(u, on_progress, **kw)

    d._fetch = fetch
    d.tick()
    ff.finish()
    assert seen == [{s1.id: {"percent": 40.0, "eta": "00:05"}}]
    assert d.progress() == {} and s1.path in trimmed[-1]  # the new file is kept


def test_files_and_images_are_left_alone(tmp_path):
    q, d, ff, _, _ = setup(tmp_path)
    q.add("image", "Poster", path="img.png")
    q.add("youtube", "No link")
    d.tick()
    assert ff.started == [] and [e.title for e in q.failed] == ["No link"]


def test_a_restart_resumes_the_downloads(tmp_path):
    ids = (f"e{n}" for n in itertools.count(1))
    q = PlayQueue(tmp_path / "queue.json", make_id=lambda: next(ids))
    add_songs(q, 2)
    Downloader(q, fetch=lambda *a, **k: {}, spawn=lambda fn: None, cached=lambda v: None).tick()
    assert [e.status for e in q.upcoming] == ["downloading", "downloading"]
    restarted = PlayQueue(tmp_path / "queue.json")
    ff = FakeFetch(tmp_path)
    d = Downloader(restarted, fetch=ff.fetch, spawn=ff.spawn, cached=lambda v: None, trim=lambda keep: None)
    d.tick()
    ff.finish(2)
    assert all(e.ready for e in restarted.upcoming)


def test_a_mix_downloads_and_plays_through(tmp_path):
    """Downloader and feeder together: playback waits for each song, never skips one."""
    q, d, ff, _, _ = setup(tmp_path)
    add_songs(q, 3, block=True)
    vlc = FakeVlc(lengths={f"{vid(i)}.mkv": 5 for i in (1, 2, 3)})
    f = Feeder(q, vlc)
    played = []

    def step(seconds=1):
        vlc.play_for(seconds)
        d.tick()
        f.tick()
        name = vlc.playing_name()
        if name and (not played or played[-1] != name):
            played.append(name)

    step()
    assert vlc.state == "stopped" and f.waiting()  # nothing downloaded yet
    ff.finish()  # S1
    step()
    step(6)  # S1 over, S2 still downloading
    assert vlc.state == "stopped" and f.waiting()
    ff.finish(2)
    for _ in range(15):
        step()
    assert played == [f"{vid(i)}.mkv" for i in (1, 2, 3)]


# --- youtube.download() and the cache, against the fake yt-dlp program -------------------------
@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "YTDLP", "fake")
    monkeypatch.setattr(youtube, "_base_cmd", lambda: [sys.executable, FAKE_YTDLP])
    monkeypatch.setattr(youtube, "CACHE_DIR", tmp_path / "youtube")
    return tmp_path / "youtube"


def test_download_reads_yt_dlps_output(fake_ytdlp):
    seen = []
    meta = youtube.download(url("abcdefghijk"), seen.append)
    assert Path(meta["path"]) == fake_ytdlp / "abcdefghijk.mkv"
    assert meta["title"] == "Song abcdefghijk" and meta["duration"] == 212 and meta["uploader"] == "Some Band"
    assert meta["quality"] == "1080p" and "height<=1080" in Path(meta["path"]).read_text()
    assert [p["percent"] for p in seen] == [0.0, 0.0, 50.0, 100.0]


def test_music_videos_get_the_720p_ladder(fake_ytdlp):
    meta = youtube.download("https://youtu.be/musicmusic1?feature=music")
    assert meta["quality"] == "720p (music)" and "height<=720" in Path(meta["path"]).read_text()


def test_a_failed_download_says_why(fake_ytdlp):
    with pytest.raises(Exception, match="Video unavailable"):
        youtube.download(url("badbadbad01"), cap="playlist")


def test_the_real_download_behind_the_worker(fake_ytdlp):
    q = PlayQueue()
    e = q.add("youtube", "S", url=url("abcdefghijk"), video_id="abcdefghijk", cap="playlist")
    runs = []
    d = Downloader(q, spawn=runs.append, trim=lambda keep: None)
    d.tick()
    runs.pop()()
    assert e.ready and e.path.endswith("abcdefghijk.mkv")
    # queued again later: straight from the cache, no yt-dlp
    again = q.add("youtube", "S again", url=url("abcdefghijk"), video_id="abcdefghijk")
    d.tick()
    assert again.ready and runs == []


def test_cached_file_skips_partial_downloads(fake_ytdlp):
    fake_ytdlp.mkdir()
    (fake_ytdlp / "abcdefghijk.f137.mp4").write_bytes(b"x")
    (fake_ytdlp / "abcdefghijk.mkv.part").write_bytes(b"x")
    assert youtube.cached_file("abcdefghijk") is None
    (fake_ytdlp / "abcdefghijk.mkv").write_bytes(b"x")
    assert youtube.cached_file("abcdefghijk").endswith("abcdefghijk.mkv")


def test_trim_keeps_queued_files_whatever_the_slashes(fake_ytdlp, monkeypatch):
    fake_ytdlp.mkdir()
    old = fake_ytdlp / "oldoldoldol.mkv"
    gone = fake_ytdlp / "gonegonegon.mkv"
    for f in (old, gone):
        f.write_bytes(b"x")
    monkeypatch.setattr(youtube, "CACHE_DAYS", -1)  # everything counts as too old
    youtube.trim_cache({str(old).upper()})
    assert old.exists() and not gone.exists()


def test_video_ids_from_links():
    assert youtube.video_id_of("https://youtu.be/dQw4w9WgXcQ?si=abc") == "dQw4w9WgXcQ"
    assert youtube.video_id_of("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.video_id_of("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.video_id_of("https://www.youtube.com/playlist?list=PL123") is None
