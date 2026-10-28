"""The feeder against a fake VLC: the two-item window, following VLC, waiting for
downloads, the page controls, and track 1 for YouTube."""

import itertools

from fake_vlc import FakeVlc
from feeder import Feeder
from playqueue import PlayQueue


def setup(tmp_path, lengths=None):
    ids = (f"e{n}" for n in itertools.count(1))
    q = PlayQueue(make_id=lambda: next(ids))
    vlc = FakeVlc(lengths=lengths)
    events = []
    f = Feeder(
        q,
        vlc,
        on_start=lambda e: events.append(("start", e.title)),
        on_end=lambda e, watched, length: events.append(("end", e.title, watched, length)),
    )
    return q, vlc, f, events


def file(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def owned_in_vlc(vlc, f):
    return [vlc.name(i["uri"]) for i in vlc.items if i["id"] in f.owned]


def run(f, vlc, seconds, step=1.0):
    """Let time pass with the feeder ticking every ``step`` seconds."""
    t = 0.0
    while t < seconds:
        vlc.play_for(step)
        f.tick()
        t += step


def test_plays_the_queue_in_order_holding_two_items_at_most(tmp_path):
    q, vlc, f, events = setup(tmp_path, {"a.mkv": 10, "b.mkv": 10, "c.mkv": 10})
    for n in ("a", "b", "c"):
        q.add("file", n.upper(), path=file(tmp_path, f"{n}.mkv"))
    f.tick()
    assert vlc.playing_name() == "a.mkv" and q.current.title == "A"
    played = []
    for _ in range(40):
        vlc.play_for(1)
        f.tick()
        assert len(owned_in_vlc(vlc, f)) <= 2
        if vlc.playing_name() and (not played or played[-1] != vlc.playing_name()):
            played.append(vlc.playing_name())
    assert played == ["a.mkv", "b.mkv", "c.mkv"]
    assert q.current is None and [e.title for e in q.history] == ["A", "B", "C"]
    assert [e for e in events if e[0] == "start"] == [("start", "A"), ("start", "B"), ("start", "C")]


def test_the_next_entry_is_preloaded_right_after_the_playing_one(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    for n in ("a", "b", "c"):
        q.add("file", n, path=file(tmp_path, f"{n}.mkv"))
    f.tick()
    f.tick()
    assert vlc.names() == ["a.mkv", "b.mkv"]  # c waits in the app's queue, not in VLC


def test_a_block_waits_for_its_next_song(tmp_path):
    q, vlc, f, _ = setup(tmp_path, {"s1.mkv": 5})
    block, (s1, s2) = q.add_block(
        "Mix", "mix", [{"kind": "youtube", "title": "S1"}, {"kind": "youtube", "title": "S2"}]
    )
    q.set_status(s1.id, "ready", path=file(tmp_path, "s1.mkv"))
    f.tick()
    assert vlc.playing_name() == "s1.mkv"
    run(f, vlc, 8)
    assert vlc.state == "stopped" and q.current is None
    assert f.waiting() and [e.title for e in q.upcoming] == ["S2"]
    q.set_status(s2.id, "ready", path=file(tmp_path, "s2.mkv"))
    f.tick()
    assert vlc.playing_name() == "s2.mkv" and q.current.title == "S2" and not f.waiting()


def test_a_reorder_swaps_the_preloaded_item(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    a = q.add("file", "A", path=file(tmp_path, "a.mkv"))
    b = q.add("file", "B", path=file(tmp_path, "b.mkv"))
    c = q.add("file", "C", path=file(tmp_path, "c.mkv"))
    f.tick()
    f.tick()
    assert vlc.names() == ["a.mkv", "b.mkv"]
    q.reorder([c.id, b.id])
    f.tick()
    assert vlc.names() == ["a.mkv", "c.mkv"]


def test_vlc_moving_on_to_a_stale_preload_still_follows(tmp_path):
    q, vlc, f, _ = setup(tmp_path, {"a.mkv": 5})
    a = q.add("file", "A", path=file(tmp_path, "a.mkv"))
    b = q.add("file", "B", path=file(tmp_path, "b.mkv"))
    c = q.add("file", "C", path=file(tmp_path, "c.mkv"))
    f.tick()
    f.tick()  # b preloaded
    q.reorder([c.id, b.id])  # ...and A ends before the feeder swaps it
    vlc.play_for(6)
    f.tick()
    assert vlc.playing_name() == "b.mkv" and q.current.title == "B"
    assert [e.title for e in q.upcoming] == ["C"] and [e.title for e in q.history] == ["A"]


def test_next_at_the_projector_is_a_skip(tmp_path):
    q, vlc, f, events = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    f.tick()
    run(f, vlc, 20)
    vlc("status.json", command="pl_next")
    f.tick()
    assert q.current.title == "B" and [e.title for e in q.history] == ["A"]
    assert ("end", "A", 20.0, 100.0) in events


def test_stop_at_the_projector_holds_the_queue(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    f.tick()
    run(f, vlc, 10)
    vlc("status.json", command="pl_stop")
    for _ in range(3):
        f.tick()
    assert vlc.state == "stopped" and q.current is None
    assert [e.title for e in q.upcoming] == ["A", "B"] and not f.waiting()
    f.play()
    assert vlc.playing_name() == "a.mkv" and q.current.title == "A"


def test_page_stop_then_play_starts_the_same_item_again(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    f.tick()
    run(f, vlc, 10)
    f.stop()
    f.tick()
    f.tick()
    assert vlc.state == "stopped" and [e.title for e in q.upcoming] == ["A", "B"]
    f.play()
    assert vlc.playing_name() == "a.mkv" and vlc.time == 0


def test_page_next_waits_when_the_next_song_is_still_downloading(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    pending = q.add("youtube", "Song", url="https://youtu.be/x")
    f.tick()
    f.next()
    assert vlc.state == "stopped" and q.current is None and f.waiting()
    q.set_status(pending.id, "ready", path=file(tmp_path, "youtube_x.mkv"))
    f.tick()
    assert q.current.title == "Song"


def test_page_next_with_a_preloaded_entry(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    f.tick()
    f.tick()
    f.next()
    f.tick()
    assert vlc.playing_name() == "b.mkv" and q.current.title == "B"


def test_previous_replays_and_then_carries_on(tmp_path):
    q, vlc, f, _ = setup(tmp_path, {"a.mkv": 5, "again": 5})
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    q.add("file", "C", path=file(tmp_path, "c.mkv"))
    f.tick()
    run(f, vlc, 7)  # A finished, B playing
    assert q.current.title == "B"
    f.previous()
    assert vlc.playing_name() == "a.mkv" and q.current.title == "A"
    f.tick()
    # B comes next, and VLC holds it after A so it moves on by itself
    names = vlc.names()
    assert names.index("b.mkv") > names.index("a.mkv") and q.peek().title == "B"
    run(f, vlc, 6)
    assert q.current.title == "B"


def test_youtube_items_get_their_first_audio_track(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("youtube", "Song", path=file(tmp_path, "youtube_song.mkv"))
    f.tick()  # starts; tracks checked on the same tick
    assert vlc.audio[vlc.current] == 1


def test_library_files_are_left_to_the_track_memory(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "Movie", path=file(tmp_path, "movie.mkv"))
    f.tick()
    assert not [c for c in vlc.calls if c[0] == "tracks.json"]


def test_image_options_go_to_vlc(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("image", "Poster", path=file(tmp_path, "img.png"), meta={"options": ["image-duration=12"]})
    f.tick()
    assert vlc.items[0]["options"] == ["image-duration=12"]


def test_items_dragged_in_at_the_projector_are_left_alone(tmp_path):
    q, vlc, f, _ = setup(tmp_path)
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    f.tick()
    foreign = vlc.add_foreign((tmp_path / "home_video.mkv").as_uri())
    for _ in range(3):
        f.tick()
    assert foreign in [i["id"] for i in vlc.items] and foreign not in f.owned


def test_restart_picks_up_the_saved_queue(tmp_path):
    ids = (f"e{n}" for n in itertools.count(1))
    q = PlayQueue(tmp_path / "queue.json", make_id=lambda: next(ids))
    q.add("file", "A", path=file(tmp_path, "a.mkv"))
    q.add("file", "B", path=file(tmp_path, "b.mkv"))
    restarted = PlayQueue(tmp_path / "queue.json")
    vlc = FakeVlc()
    Feeder(restarted, vlc).tick()
    assert vlc.playing_name() == "a.mkv"
