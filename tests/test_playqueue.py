"""The queue's rules (docs/queue_rebuild.md), including every bug that made the
old Mix/playlist feature get rebuilt."""

import itertools
import json

import pytest

from playqueue import PlayQueue, normalize


def make_queue(tmp_path=None):
    ids = (f"e{n}" for n in itertools.count(1))
    clock = itertools.count(1000)
    return PlayQueue(
        tmp_path / "queue.json" if tmp_path else None,
        clock=lambda: next(clock),
        make_id=lambda: next(ids),
    )


def song(title, path=None, **kw):
    return {"kind": "youtube", "title": title, "path": path, **kw}


def order(q):
    return [e.title for e in q.upcoming]


def mix(q, label="Mix - Queen", n=3, ready=True, by="Sam"):
    items = [song(f"{label} {i}", f"cache/youtube/{label}{i}.mkv" if ready else None) for i in range(1, n + 1)]
    return q.add_block(label, "mix", items, queued_by=by, source_url="https://youtube.com/watch?v=x&list=RDx")


# --- the bugs that started the rebuild ---------------------------------------------------------
def test_solo_song_from_a_mix_stays_solo_even_sharing_the_file():
    q = make_queue()
    block, entries = mix(q)
    shared = entries[1].path
    solo = q.add("youtube", "Mix - Queen 2", path=shared, queued_by="Alex")
    assert solo.block is None and solo.queued_by == "Alex"
    assert [e.block for e in q.upcoming] == [block.id] * 3 + [None]


def test_removed_mix_never_comes_back():
    q = make_queue()
    old, old_entries = mix(q)
    q.remove_block(old.id)
    assert q.upcoming == [] and old.id not in q.blocks
    # the same files queued again (solo and as a new Mix) don't revive the old block
    q.add("youtube", "again", path=old_entries[0].path)
    new, _ = mix(q)
    assert new.id != old.id and old.id not in q.blocks
    assert {e.block for e in q.upcoming} == {None, new.id}


def test_a_mix_is_one_block_not_single_songs():
    q = make_queue()
    block, entries = mix(q, n=25)
    assert len({e.block for e in entries}) == 1 and block.size == 25
    snap = q.snapshot()
    assert {r["block_label"] for r in snap["upcoming"]} == {"Mix - Queen"}


def test_moving_an_item_never_rebuilds_or_swaps_a_block():
    q = make_queue()
    a, a_entries = mix(q, "Mix - A")
    movie = q.add("file", "Movie", path="D:/Movies/m.mkv")
    b, b_entries = mix(q, "Mix - B")
    before = {e.id: (e.block, e.path) for e in q.upcoming}
    q.reorder([movie.id] + [e.id for e in a_entries] + [e.id for e in b_entries])
    assert order(q)[0] == "Movie"
    # same entries, same blocks, same files: only the order changed
    assert {e.id: (e.block, e.path) for e in q.upcoming} == before


# --- blocks -----------------------------------------------------------------------------------
def test_every_add_is_a_new_block_even_for_the_same_url():
    q = make_queue()
    first, _ = mix(q)
    second, _ = mix(q)
    assert first.id != second.id and len(q.blocks) == 2


def test_reorder_inside_a_block():
    q = make_queue()
    block, (s1, s2, s3) = mix(q)
    assert q.reorder([s3.id, s1.id, s2.id])
    assert order(q) == ["Mix - Queen 3", "Mix - Queen 1", "Mix - Queen 2"]


def test_a_song_dragged_out_of_its_block_is_put_back():
    q = make_queue()
    movie = q.add("file", "Movie", path="m.mkv")
    block, (s1, s2, s3) = mix(q)
    # drag the last song to the top, past the movie: the block can't be split
    q.reorder([s3.id, movie.id, s1.id, s2.id])
    titles = order(q)
    assert titles == ["Mix - Queen 3", "Mix - Queen 1", "Mix - Queen 2", "Movie"]
    # ...the whole block moved, skipping the movie, with the dragged song first


def test_nothing_can_land_inside_a_block():
    q = make_queue()
    block, (s1, s2, s3) = mix(q)
    show = q.add("file", "Episode", path="ep.mkv")
    q.reorder([s1.id, show.id, s2.id, s3.id])
    assert order(q) == ["Mix - Queen 1", "Mix - Queen 2", "Mix - Queen 3", "Episode"]


def test_the_playing_block_stays_at_the_front():
    q = make_queue()
    block, (s1, s2, s3) = mix(q)
    movie = q.add("file", "Movie", path="m.mkv")
    assert q.advance() is s1
    q.reorder([movie.id, s2.id, s3.id])
    assert order(q) == ["Mix - Queen 2", "Mix - Queen 3", "Movie"]


def test_removing_single_songs_and_the_block_ends_with_its_last_song():
    q = make_queue()
    block, (s1, s2, s3) = mix(q)
    q.remove(s2.id)
    assert order(q) == ["Mix - Queen 1", "Mix - Queen 3"] and block.id in q.blocks
    q.advance()  # s1 playing
    q.remove(s3.id)
    assert block.id in q.blocks  # still playing its first song
    q.advance()  # nothing left
    assert q.current is None and block.id not in q.blocks


def test_removing_an_entry_never_touches_another_with_the_same_file():
    q = make_queue()
    a = q.add("youtube", "Song", path="cache/youtube/x.mkv")
    b = q.add("youtube", "Song", path="cache/youtube/x.mkv")
    q.remove(a.id)
    assert [e.id for e in q.upcoming] == [b.id]


def test_reorder_tolerates_a_page_that_is_behind():
    q = make_queue()
    a = q.add("file", "A", path="a")
    b = q.add("file", "B", path="b")
    c = q.add("file", "C", path="c")  # the page hasn't seen C yet
    assert q.reorder([b.id, a.id, "gone-already"])
    assert order(q) == ["B", "A", "C"]
    assert not q.reorder([b.id, a.id, c.id])


def test_normalize_keeps_solo_places():
    q = make_queue()
    x = q.add("file", "X", path="x")
    block, (s1, s2) = mix(q, n=2)
    y = q.add("file", "Y", path="y")
    assert [e.title for e in normalize([s2, x, y, s1])] == ["Mix - Queen 2", "Mix - Queen 1", "X", "Y"]


# --- downloads and playback ---------------------------------------------------------------------
def test_downloads_in_queue_order_two_at_a_time_and_follow_reorders():
    q = make_queue()
    block, entries = mix(q, n=4, ready=False)
    single = q.add("youtube", "Single", url="https://youtu.be/s")
    first = q.to_download(2)
    assert [e.title for e in first] == ["Mix - Queen 1", "Mix - Queen 2"]
    for e in first:
        q.set_status(e.id, "downloading")
    assert q.to_download(2) == []
    # the single is dragged above the Mix: it downloads next
    q.reorder([single.id] + [e.id for e in entries])
    q.set_status(entries[0].id, "ready", path="cache/youtube/1.mkv")
    assert [e.title for e in q.to_download(2)] == ["Single"]


def test_playback_waits_for_the_next_song_of_a_block():
    q = make_queue()
    block, (s1, s2) = mix(q, n=2, ready=False)
    q.set_status(s1.id, "ready", path="1.mkv")
    assert q.advance() is s1
    assert q.peek() is None  # s2 still downloading: wait, don't skip ahead
    q.advance()
    assert q.current is None and order(q) == ["Mix - Queen 2"]
    q.set_status(s2.id, "ready", path="2.mkv")
    assert q.advance() is s2


def test_a_failed_song_drops_out_and_the_block_carries_on():
    q = make_queue()
    block, (s1, s2, s3) = mix(q, ready=False)
    q.set_status(s2.id, "failed", error="Video unavailable")
    assert order(q) == ["Mix - Queen 1", "Mix - Queen 3"]
    assert [e.title for e in q.failed] == ["Mix - Queen 2"] and block.id in q.blocks
    q.set_status(s1.id, "failed", error="x")
    q.set_status(s3.id, "failed", error="x")
    assert block.id not in q.blocks  # nothing left of it


def test_ready_needs_a_path_and_unknown_entries_are_ignored():
    q = make_queue()
    e = q.add("youtube", "S", url="u")
    with pytest.raises(ValueError):
        q.set_status(e.id, "ready")
    assert q.set_status("removed-meanwhile", "ready", path="p") is None


def test_previous_brings_the_last_song_back_as_solo():
    q = make_queue()
    block, (s1, s2, s3) = mix(q)
    q.advance()
    q.advance()  # s1 played, s2 playing
    again = q.previous()
    assert again.block is None and again.id != s1.id and again.path == s1.path
    assert order(q) == ["Mix - Queen 1", "Mix - Queen 2", "Mix - Queen 3"]
    assert q.advance() is again


def test_previous_steps_further_back_each_press():
    q = make_queue()
    for name in ("F1", "F2", "F3"):
        q.add("file", name, path=name)
    for _ in range(3):
        q.advance()  # F3 playing
    q.previous()
    assert q.advance().title == "F2"
    q.previous()
    assert q.advance().title == "F1"
    assert order(q) == ["F2", "F3"]  # each interrupted item went back to the front, in order
    assert q.previous() is None and q.current.title == "F1"


def test_history_keeps_the_last_twenty():
    q = make_queue()
    for i in range(25):
        q.add("file", f"F{i}", path=f"f{i}")
    for _ in range(26):
        q.advance()
    assert len(q.history) == 20 and q.history[-1].title == "F24"


def test_in_use_protects_queued_files_from_the_cache():
    q = make_queue()
    q.add("youtube", "A", path="cache/youtube/a.mkv")
    q.add("youtube", "B", url="u")
    assert q.in_use() == {"cache/youtube/a.mkv"}


# --- saving, restart, cutover -------------------------------------------------------------------
def test_restart_restores_everything_and_resumes_downloads(tmp_path):
    q = make_queue(tmp_path)
    block, (s1, s2, s3) = mix(q, ready=False)
    q.set_status(s1.id, "ready", path="1.mkv")
    q.set_status(s2.id, "downloading")
    q.add("image", "Poster", path="img.png", queued_by="Jo")
    q.advance()
    restarted = PlayQueue(tmp_path / "queue.json")
    assert restarted.current.id == s1.id and restarted.current.block == block.id
    assert [e.id for e in restarted.upcoming] == [e.id for e in q.upcoming]
    assert [e.status for e in restarted.upcoming] == ["pending", "pending", "ready"]  # s2 resumes
    assert restarted.blocks[block.id].label == "Mix - Queen"


def test_save_is_plain_json(tmp_path):
    q = make_queue(tmp_path)
    q.add("file", "A", path="a")
    data = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["upcoming"][0]["title"] == "A"
    assert not (tmp_path / "queue.json.tmp").exists()


def test_adopt_only_into_an_empty_queue():
    q = make_queue()
    got = q.adopt([{"title": "Was in VLC", "path": "a.mkv", "block": "old"}, {"title": "B", "path": "b.mkv"}])
    assert [e.block for e in got] == [None, None] and order(q) == ["Was in VLC", "B"]
    assert q.adopt([{"title": "C", "path": "c"}]) == []


def test_bad_kinds_are_refused():
    q = make_queue()
    with pytest.raises(ValueError):
        q.add("podcast", "x")
    with pytest.raises(ValueError):
        q.add_block("x", "album", [song("a", "a")])
    with pytest.raises(ValueError):
        q.add_block("x", "mix", [])


def test_begin_takes_an_entry_from_anywhere_and_keeps_its_block_first():
    q = make_queue()
    movie = q.add("file", "Movie", path="m")
    block, (s1, s2) = mix(q, n=2)
    assert q.begin(s1.id) is s1  # VLC played the preloaded song after a reorder put the movie first
    assert order(q) == ["Mix - Queen 2", "Movie"]
    assert q.begin("not-queued") is None and q.current is s1
    q.begin(s2.id)
    assert [e.title for e in q.history] == ["Mix - Queen 1"]


def test_requeue_current_puts_it_back_at_the_front():
    q = make_queue()
    q.add("file", "A", path="a")
    q.add("file", "B", path="b")
    q.advance()
    assert q.requeue_current().title == "A"
    assert q.current is None and order(q) == ["A", "B"] and not q.history
    assert q.requeue_current() is None
