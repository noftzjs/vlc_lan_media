"""The routes on the new queue, through FastAPI's test client, with a fake VLC and fake
downloads: the bugs that started the rebuild, end to end, plus the first-start adoption."""

import itertools
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Importing app reads the queue file and resolves the library folders (the NAS, which can hang
# when it isn't mapped): point both at a scratch folder first.
_scratch = Path(tempfile.mkdtemp())
os.environ.setdefault("QUEUE_FILE", str(_scratch / "queue.json"))
os.environ["MEDIA_DIRS"] = str(_scratch / "Movies")

from fake_vlc import FakeVlc
from test_downloader import FakeFetch

import app
from feeder import Feeder
from playqueue import PlayQueue
from youtube import Downloader

MIX_URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa&list=RDaaaaaaaaaaa"


def video(n: int) -> dict:
    vid = f"song{n:07d}"
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": f"Song {n}",
            "uploader": "Queen", "duration": 200, "thumbnail": f"https://i.ytimg.com/vi/{vid}/mq.jpg"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    ids = (f"e{n}" for n in itertools.count(1))
    q = PlayQueue(tmp_path / "queue.json", make_id=lambda: next(ids))
    vlc = FakeVlc(default_length=100)
    ff = FakeFetch(tmp_path)
    feeder = Feeder(q, vlc, on_end=app.playback_ended)
    downloader = Downloader(q, fetch=ff.fetch, kill=ff.kill, cached=lambda v: None, trim=lambda keep: None,
                            spawn=ff.spawn, on_finish=app.download_finished)
    for name, value in {
        "play_queue": q, "feeder": feeder, "downloader": downloader, "vlc": vlc,
        "QUEUE_FILE": tmp_path / "queue.json", "EVENT_LOG": tmp_path / "events.jsonl",
        "REQUESTERS_FILE": tmp_path / "requesters.json", "EXTRA_ITEMS_FILE": tmp_path / "extra.json",
        "requesters": {}, "extra_items": {}, "recent_failures": {}, "first_roll": {"pending": False},
    }.items():
        monkeypatch.setattr(app, name, value)
    monkeypatch.setattr(app.youtube, "available", lambda: True)
    monkeypatch.setattr(app.youtube, "CACHE_DIR", tmp_path / "youtube")
    mix = [video(n) for n in (1, 2, 3)]
    monkeypatch.setattr(app.youtube, "playlist", lambda url: {
        "title": "Mix - Queen", "url": url, "count": 3, "mix": True, "entries": [dict(v) for v in mix]})

    def library_file(item_id):
        path = tmp_path / "media" / item_id.split("/", 1)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(app.library, "resolve", library_file)
    return SimpleNamespace(q=q, vlc=vlc, ff=ff, feeder=feeder, downloader=downloader, tmp=tmp_path,
                           client=TestClient(app.app))


def rows(env):
    return env.client.get("/api/status").json()["queue"]


def events(env, kind=None):
    path = env.tmp / "events.jsonl"
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [r for r in recs if kind is None or r["kind"] == kind]


def queue_mix(env, user="Sam"):
    r = env.client.post("/api/queue/youtube", json={"url": MIX_URL, "user": user})
    assert r.status_code == 200, r.text
    return r.json()["block"]


# --- the bugs that started the rebuild, through the routes ------------------------------------------
def test_a_mix_is_one_block_and_plays_as_it_downloads(env):
    block = queue_mix(env)
    queue = rows(env)
    assert [r["group"] for r in queue] == [block] * 3
    assert {r["group_label"] for r in queue} == {"Mix - Queen"} and queue[0]["block_kind"] == "mix"
    assert [r["status"] for r in queue] == ["downloading", "downloading", "pending"]
    assert queue[2]["name"] == "Waiting to download" and queue[0]["queued_by"] == "Sam"
    env.ff.finish()  # Song 1 downloaded
    env.feeder.tick()
    queue = rows(env)
    assert queue[0]["current"] and queue[0]["title"] == "Song 1" and env.vlc.playing_name() == "song0000001.mkv"
    logged = events(env, "youtube")
    assert logged[0]["block_id"] == block and logged[0]["playlist_index"] == 1 and logged[0]["name"] == "Sam"


def test_a_solo_song_sharing_a_mix_file_stays_solo(env):
    block = queue_mix(env)
    env.client.post("/api/queue/youtube", json={"url": video(2)["url"], "user": "Alex", "title": "Song 2"})
    solo = rows(env)[-1]
    assert solo["group"] is None and solo["queued_by"] == "Alex" and solo["status"] == "downloading"
    assert [r["group"] for r in rows(env)[:3]] == [block] * 3
    assert len(env.downloader.jobs) == 2  # Song 2 downloads once for both entries


def test_a_removed_mix_never_comes_back(env):
    first = queue_mix(env)
    second = queue_mix(env)
    assert first != second
    r = env.client.delete(f"/api/queue/group/{first}")
    assert r.json()["message"] == "Removed 3 items: Mix - Queen"
    assert {row["group"] for row in rows(env)} == {second}
    again = queue_mix(env)
    assert again not in (first, second) and {row["group"] for row in rows(env)} == {second, again}
    assert env.client.delete(f"/api/queue/group/{first}").status_code == 404
    assert events(env, "remove")[0]["block_id"] == first


def test_a_reorder_keeps_blocks_together(env):
    env.client.post("/api/queue", json={"id": "Movies/movie.mkv", "user": "Jo"})  # starts playing
    env.client.post("/api/queue", json={"id": "Movies/next.mkv", "user": "Jo"})
    block = queue_mix(env)
    queue = rows(env)
    nxt, s1, s2, s3 = (r["id"] for r in queue[1:])
    # drag the last song to the top, past the movie: the whole block moves, the song first
    r = env.client.post("/api/queue/reorder", json={"ids": [s3, nxt, s1, s2]})
    titles = [row["title"] for row in r.json()["queue"]]
    assert titles == ["movie", "Song 3", "Song 1", "Song 2", "next"]
    assert [row["group"] for row in r.json()["queue"][1:4]] == [block] * 3
    assert events(env, "reorder")


def test_removing_a_downloading_song_stops_its_download(env):
    queue_mix(env)
    first = rows(env)[0]
    env.client.delete(f"/api/queue/{first['id']}")
    assert len(env.ff.killed) == 1
    assert [r["title"] for r in rows(env)] == ["Song 2", "Song 3"]


def test_removing_the_playing_entry_is_a_skip(env):
    env.client.post("/api/queue", json={"id": "Movies/a.mkv"})
    env.client.post("/api/queue", json={"id": "Movies/b.mkv"})
    playing = rows(env)[0]
    assert playing["current"] and playing["title"] == "a"
    env.client.delete(f"/api/queue/{playing['id']}")
    env.feeder.tick()
    assert rows(env)[0]["title"] == "b" and env.vlc.playing_name() == "b.mkv"
    assert events(env, "remove")[0]["was_playing"] is True


# --- library, dice, controls, status ------------------------------------------------------------------
def test_a_season_is_one_block_and_starts_an_idle_vlc(env):
    r = env.client.post("/api/queue/batch", json={"ids": ["TV/s1e1.mkv", "TV/s1e2.mkv"], "user": "Jo",
                                                  "label": "Show · Season 1"})
    assert r.json()["message"] == "Now playing: 2 episodes"
    queue = rows(env)
    assert queue[0]["current"] and queue[1]["group_label"] == "Show · Season 1"
    assert queue[1]["block_kind"] == "season" and queue[1]["queued_by"] == "Jo"
    assert {e["block_id"] for e in events(env, "library")} == {queue[0]["group"]}


def test_a_youtube_roll_is_solo_and_credited_to_claude(env):
    cached = env.tmp / "youtube" / "song0000002.mkv"
    cached.parent.mkdir()
    cached.write_bytes(b"x")
    app.extra_items[str(cached).lower()] = {"title": "Song 2", "youtube": video(2)["url"], "poster": None}
    queue_mix(env)
    r = env.client.post("/api/queue/random", json={"kind": "youtube", "user": "Sam"})
    assert r.status_code == 200, r.text
    roll = rows(env)[-1]
    assert roll["group"] is None and roll["queued_by"] == "Claude" and roll["status"] == "ready"
    assert events(env, "youtube")[-1]["name"] == "Sam"  # the log keeps the real requester


def test_stop_holds_the_queue_and_play_resumes(env):
    env.client.post("/api/queue", json={"id": "Movies/a.mkv"})
    env.client.post("/api/queue", json={"id": "Movies/b.mkv"})
    env.client.post("/api/control", json={"action": "stop"})
    env.feeder.tick()
    assert env.vlc.state == "stopped" and [r["title"] for r in rows(env)] == ["a", "b"]
    env.client.post("/api/queue", json={"id": "Movies/c.mkv"})  # queueing doesn't override a stop
    assert env.vlc.state == "stopped"
    env.client.post("/api/control", json={"action": "play"})
    assert env.vlc.playing_name() == "a.mkv"
    assert env.client.post("/api/control", json={"action": "rewind"}).status_code == 400


def test_the_queue_shows_while_vlc_is_down(env, monkeypatch):
    queue_mix(env)

    def down(*a, **k):
        raise HTTPException(status_code=503, detail="VLC is not running")

    monkeypatch.setattr(app, "vlc", down)
    s = env.client.get("/api/status").json()
    assert s["state"] == "unavailable" and s["vlc_error"] == "VLC is not running" and len(s["queue"]) == 3


def test_a_failed_download_shows_then_can_be_dismissed(env):
    env.ff.fail[video(1)["url"]] = "Video unavailable"
    queue_mix(env)
    env.ff.finish()
    s = env.client.get("/api/status").json()
    assert [r["title"] for r in s["queue"]] == ["Song 2", "Song 3"]
    assert [(p["title"], p["error"]) for p in s["pending"]] == [("Song 1", "Video unavailable")]
    env.client.delete(f"/api/youtube/jobs/{s['pending'][0]['id']}")
    assert env.client.get("/api/status").json()["pending"] == []
    assert events(env, "youtube")[0]["failed"] is True


def test_playback_end_is_logged_with_its_block(env):
    block = queue_mix(env)
    env.ff.finish()
    env.feeder.tick()
    env.vlc.play_for(10)
    env.feeder.tick()
    env.client.post("/api/control", json={"action": "next"})
    playback = events(env, "playback")
    assert playback[0]["event"] == "skipped" and playback[0]["watched_s"] == 10
    assert playback[0]["block_id"] == block and playback[0]["via"] == "next"


# --- first start of the new build ------------------------------------------------------------------------
def test_first_start_adopts_vlcs_playlist_once(env):
    media = env.tmp / "media"
    media.mkdir()
    files = [media / n for n in ("playing.mkv", "next.mkv", "song.mkv")]
    for f in files:
        f.write_bytes(b"x")
    app.extra_items[app._path_key(str(files[2]))] = {
        "title": "A Song", "name": "Queen", "youtube": video(5)["url"], "queued_by": "Sam", "options": []}
    vlc_ids = [env.vlc.add_foreign(f.as_uri()) for f in files]
    env.vlc("status.json", command="pl_play", id=vlc_ids[0])
    app.adopt_vlc_playlist()
    adopted = env.q.upcoming
    assert [(e.kind, e.title, e.block) for e in adopted] == [("file", "next", None), ("youtube", "A Song", None)]
    assert adopted[1].video_id == "song0000005" and adopted[1].queued_by == "Sam"
    assert env.vlc.names() == ["playing.mkv"] and env.vlc.playing_name() == "playing.mkv"
    env.feeder.tick()  # the next entry goes in after the item still playing
    assert env.vlc.names() == ["playing.mkv", "next.mkv"]
    env.vlc.add_foreign(files[0].as_uri())
    app.adopt_vlc_playlist()  # the queue file exists now: never again
    assert len(env.q.upcoming) == 2
