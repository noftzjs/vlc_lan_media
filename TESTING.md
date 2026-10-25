# Testing

There is no automated suite yet; this is the pass to run after any sizable change, in the order
a party would hit it. Start with `start.ps1` so VLC runs with the custom HTTP folder (the track
dropdowns and track memory need it), then open the page on a PC browser and a phone.

## Now playing and controls
- [ ] Title, poster, state and time update while something plays; an episode shows its marker (S02E05 / E02).
- [ ] Previous, Play/Pause, Next, Stop, Fullscreen all act on VLC.
- [ ] Audio and Subtitles dropdowns appear for a file with more than one track; switching applies live.
- [ ] Track memory: pick an audio track on an episode, play the next episode of the same show, the choice sticks.

## Queue
- [ ] Queue a single movie; it appears at the bottom with poster, title and "queued by" your name.
- [ ] Queue with the name box empty from the same PC: still credited to the name used before.
- [ ] Drag a loose item up and down; drag one to the very top (it lands right under the playing item).
- [ ] Reorder while a playlist is still downloading (ids change underneath): no "queue changed" error.
- [ ] Remove an upcoming item with ✕; remove the playing item (VLC moves on).
- [ ] Runtime total and "N queued" in the Up next header are right.

## Blocks (seasons and playlists)
- [ ] Queue season: one block with the season label, item count, runtime, "queued by".
- [ ] Queue all on a show whose season folders contain the show's name: header doesn't repeat the title.
- [ ] Drag the block as a unit; drag an episode inside the block; an episode can't be dragged out of it.
- [ ] Collapse and expand the block; ✕ on the block removes all its upcoming items.
- [ ] The playing episode sits above the block as the pinned row; the rest stay grouped.

## YouTube
- [ ] Search by words; queue one result; it downloads (progress in Up next) and joins the queue.
- [ ] Paste a single video link; paste a playlist link (card with count); paste a Mix link (`list=RD...`, card says Mix).
- [ ] Queue a playlist: videos download one at a time, each joins the same block as it lands, in order.
- [ ] Something queued while a playlist downloads doesn't split the block (the new video moves up to its block).
- [ ] Cancel the running playlist download: the rest stop.
- [ ] Titles with non-Latin characters and accents arrive intact (【brackets】, é).
- [ ] A music video and any playlist video download at 720p; a normal video at 1080p (check the log's `quality`).
- [ ] Re-queue a video already in the cache: near-instant, no new download.

## Images
- [ ] Drop a still image with a duration; it shows for that long. An animated GIF loops for the duration.

## Secret menu
- [ ] Type `@claude` in the search box: gif and one Random button, in both the library and YouTube tabs.
- [ ] First roll after a server restart is Terminator 2, even if clicked seconds after start.
- [ ] Later rolls vary across movie / episode / anime / cached YouTube; queue shows "queued by Claude".
- [ ] A rolled cached video appears on its own, not as a one-item copy of the playlist it came from.

## Log and report
- [ ] `cache/queue_events.jsonl` gets a row per request with title, category, device (pc/phone), name, ip.
- [ ] Skip an item with Next after a minute: a `playback` row with `skipped`, watched/length and `by`.
- [ ] Let an item finish: a `finished` row.
- [ ] `python tools/queue_report.py` runs; `--markdown` prints the Discord block.

## Things that broke before (check on purpose)
- [ ] Queue item names come from the file, not VLC's embedded title tag (VLC "renames" items after pre-parsing).
- [ ] Episode rows show an episode marker, not 24 rows of the show title.
- [ ] YouTube title encoding (see above).
- [ ] Track dropdowns present (means VLC was started by `start.ps1`).
