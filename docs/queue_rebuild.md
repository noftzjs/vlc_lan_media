# Queue rebuild: YouTube videos, playlists and Mixes

Agreed 2026-10-27 in a design session, after the Mix/playlist feature kept regressing.
This is the spec the rebuild is built and reviewed against. Branch: `jn/youtube-queue-rebuild`.

## Why the old design broke

Block membership was worked out from file paths, and VLC's playlist was the queue. Every
reorder deleted and re-added everything by path, VLC renumbered its ids, and an in-memory
list of "solo" VLC ids tried to keep cached files out of the blocks they had once been in.
So a solo song joined an old Mix whenever its file was shared, a removed Mix came back as
soon as one of its files was queued again (group records were only pruned when none of
their files was in VLC), and a restart lost the solo list and any playlist still
downloading. Nothing pinned YouTube items to audio track 1.

## Scope

- Rebuilt: the whole YouTube path (single videos, playlists, Mixes) and the queue under it.
- Changed only in how they enter the queue: movies, shows, seasons, uploaded images and the
  random button. Their playback, posters and track memory are untouched.
- The page looks and works as now (search, playlist card, results kept after queueing one,
  ✓ queued tags, pending ✕, block styling), plus reordering songs inside a block.

## The queue

- **The app owns the queue.** An ordered list of entries, saved to disk after every change and
  restored exactly on restart; pending downloads resume.
- **Entries have their own identity.** Each has an id made at queue time and carries its file
  or video, who queued it, and its block id if any. Membership and credit come only from the
  entry: never from a file path or a VLC id.
- **The same video can be queued more than once** (two blocks, or solo and in a block). Separate
  entries sharing one cached file; removing one never touches the other.
- **Played entries leave the queue.** The last 20 are kept as history: ⏮ puts the most recent
  back at the front (as a solo entry) and plays it; pressing again steps further back.
- **Anyone can reorder or remove anything**, and every edit is logged with who made it.

## Blocks

- Every playlist or Mix add, and every season queued from the library, makes a **new block**.
  Nothing is matched to an earlier block, even for the same URL.
- Playlists and Mixes are **snapshotted once** at queue time, up to 25 items, and never fetched
  again (a Mix is different on every fetch).
- A block can be moved whole or removed whole; single songs can be removed; songs can be
  reordered **only inside** the block. Nothing can come between a block's songs: a drag that
  would split a block is normalized back so the block stays together.
- **Removing a block is final.** Re-queueing the same playlist makes a fresh block.
- Solo songs (a single video URL, or one pick from a playlist's results), random rolls
  (credited to "Claude"; Terminator 2 first after a restart) and images are always solo.

## VLC

- VLC holds only **the item playing and the next one**. One feeder thread is the only code
  that changes VLC's playlist; when an item starts, it hands VLC the following one.
- When the next song in a block isn't downloaded yet, **playback waits** for it (the page shows
  "downloading"). A failed download is dropped from the block, shown as failed, and the block
  carries on.
- YouTube items always play **audio track 1**, with no selector, set each time one starts.
- At the projector, VLC's next counts as a skip and stop holds the queue. Nothing else done
  directly in VLC is handled (only the operator has that machine).

## Downloads

- One worker fetches **in queue order, two at a time**: dragging an entry up moves its
  download up.
- A video already in the cache is used directly, **no yt-dlp call**. Cache limits stay at 20 GB
  and 7 days, and nothing still in the queue is trimmed.
- Quality unchanged: single videos up to 1080p; block songs and music videos capped at 720p.
- The private request log (`cache/queue_events.jsonl`) keeps its fields and adds the block id.

## Code and rollout

- `playqueue.py`: pure logic and saving (entries, blocks, ordering rules, history). No VLC or
  network calls; tested hardest.
- Feeder: the one thread that talks to VLC's playlist (two-item window, follows VLC's state,
  track 1 for YouTube).
- `youtube.py`: rewritten as the downloader around the queue-order worker.
- `app.py`: thin routes. The old group code, solo tracking, rebuild/keep-together logic and
  group pruning are deleted, not left beside the new code.
- First start of the new build: whatever is in VLC's playlist is adopted as solo entries, in
  order. Old blocks are not carried across.
- Tests: a small pytest suite (fake VLC, fake yt-dlp) covering this feature and every bug
  above. TESTING.md stays the manual run before a LAN.
- Cut over only after the tests and a full TESTING.md run pass. **If not passed by
  2026-11-02, the LAN (2026-11-04) runs on the current build with playlist and Mix queueing
  switched off, single videos only.**
