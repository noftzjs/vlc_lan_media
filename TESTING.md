# Testing

`python -m pytest` runs the automated suite (the queue, feeder, downloader and routes against a
fake VLC and a fake yt-dlp). This is the manual pass on top of it, to run after any sizable change
and before every LAN, in the order a party would hit it. Start with `start.ps1` so VLC runs with the custom HTTP folder (the track
dropdowns and track memory need it), then open the page on a PC browser and a phone.

## Now playing and controls
- [ ] Title, poster, state and time update while something plays; an episode shows its marker (S02E05 / E02).
- [ ] Previous, Play/Pause, Next, Stop, Fullscreen all act on VLC.
- [ ] Stop holds the queue: the stopped item stays first, nothing starts until Play, Play starts it again.
- [ ] Previous replays the last finished item, and pressing it again steps further back.
- [ ] At the projector: VLC's own Next skips to the next queue item; its Stop holds the queue like the page's.
- [ ] Let the last item in the queue end: VLC stops, nothing restarts on its own, queueing something plays it.
- [ ] Audio and Subtitles dropdowns appear for a file with more than one track; switching applies live.
- [ ] Track memory: pick an audio track on an episode, play the next episode of the same show, the choice sticks.
- [ ] A YouTube item plays with sound on track 1 every time, and shows no track dropdown.

## Queue
- [ ] Queue a single movie; it appears at the bottom with poster, title and "queued by" your name.
- [ ] Queue with the name box empty from the same PC: still credited to the name used before.
- [ ] Drag a loose item up and down; drag one to the very top (it lands right under the playing item).
- [ ] Reorder while a playlist is still downloading: no error, nothing jumps.
- [ ] Remove an upcoming item with ✕; remove the playing item (VLC moves on).
- [ ] VLC's own playlist only ever holds the playing item and the next one.
- [ ] Drag an image or an episode into the playing slot's next place: VLC plays that one next.
- [ ] Runtime total and "N queued" in the Up next header are right.

## Blocks (seasons and playlists)
- [ ] Queue season: one block with the season label, item count, runtime, "queued by".
- [ ] Queue all on a show whose season folders contain the show's name: header doesn't repeat the title.
- [ ] Drag the block as a unit; drag an episode inside the block; an episode can't be dragged out of it.
- [ ] Collapse and expand the block; ✕ on the block removes all its upcoming items.
- [ ] The playing episode sits above the block as the pinned row; the rest stay grouped.

## YouTube
- [ ] Search by words; queue one result: it is in Up next at once ("Waiting to download", then a progress
      bar), and plays once downloaded.
- [ ] Paste a single video link; paste a playlist link (card with count); paste a Mix link (`list=RD...`, card says Mix).
- [ ] Queue a Mix: one block at once with all its songs (up to 25), header says how many are downloading;
      two download at a time, top first.
- [ ] Drag a waiting song up past another: it downloads next.
- [ ] The Mix reaches a song still downloading: the page says "waiting for the next download to finish", and
      it plays as soon as it lands (no song skipped).
- [ ] ✕ on a song still downloading: it leaves and its download stops.
- [ ] A video that can't be downloaded shows "Download failed" with the reason, drops out of its block, and the
      block carries on; ✕ on the notice dismisses it.
- [ ] Titles with non-Latin characters and accents arrive intact (【brackets】, é).
- [ ] A music video and any playlist video download at 720p; a normal video at 1080p (check the log's `quality`).
- [ ] Re-queue a video already in the cache: ready at once, no download.

## The bugs that started the rebuild (docs/queue_rebuild.md)
- [ ] Queue a Mix, then one of its songs on its own from the search results: the single is its own row,
      credited to whoever queued it, not part of the Mix.
- [ ] Queue the same Mix twice: two blocks. ✕ one: only that one goes.
- [ ] ✕ a Mix, then queue one of its songs on its own, and queue the Mix again: the removed Mix never comes
      back; the new one is a fresh block.
- [ ] Move a movie above a Mix, and a song inside the Mix: no Mix is rebuilt, swapped or split.
- [ ] Drag a song out of its block to the top: the whole block moves with it, the song first.
- [ ] Restart the server (not VLC) mid-Mix: the queue, its blocks and credits come back; downloads resume.

## Images
- [ ] Drop a still image with a duration; it shows for that long. An animated GIF loops for the duration.

## Secret menu
- [ ] Type `@claude` in the search box: gif and one Random button, in both the library and YouTube tabs.
- [ ] First roll after a server restart is Terminator 2, even if clicked seconds after start.
- [ ] Later rolls vary across movie / episode / anime / cached YouTube; queue shows "queued by Claude".
- [ ] A rolled cached video appears on its own, not as a one-item copy of the playlist it came from.

## Log and report
- [ ] `cache/queue_events.jsonl` gets a row per request with title, category, device (pc/phone), name, ip,
      and `block_id` for Mix, playlist and season items.
- [ ] Reorders and block removals are logged with who made them.
- [ ] Skip an item with Next after a minute: a `playback` row with `skipped`, watched/length and `by`.
- [ ] Let an item finish: a `finished` row.
- [ ] `python tools/queue_report.py` runs; `--markdown` prints the Discord block.

## Things that broke before (check on purpose)
- [ ] Queue item names come from the file, not VLC's embedded title tag (VLC "renames" items after pre-parsing).
- [ ] Episode rows show an episode marker, not 24 rows of the show title.
- [ ] YouTube title encoding (see above).
- [ ] Track dropdowns present (means VLC was started by `start.ps1`).

## Cutover (first start of the queue rebuild, once)
- [ ] With the old build's queue still in VLC, start the new build: those items show up in Up next as
      single rows, in order; whatever was playing keeps playing; `cache/queue.json` now exists.
- [ ] Restart again: nothing is adopted twice.
