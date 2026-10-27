# Backlog

## Next up
Queue rebuild (spec: docs/queue_rebuild.md, branch jn/youtube-queue-rebuild; cut over only after tests + full TESTING.md; fallback cutoff 2026-11-02)
- [x] Design agreed in /grilling (2026-10-27)
- [x] `playqueue.py`: entries, blocks, ordering rules, history, saving, with tests
- [ ] Feeder: the one thread that changes VLC's playlist (two-item window, follows VLC, track 1 for YouTube), tested against a fake VLC
- [ ] Downloader: youtube.py around the queue-order worker (two at a time, cached files skip yt-dlp, 720p cap rules), tested against a fake yt-dlp
- [ ] Routes in app.py on the new queue; delete the old group code, solo tracking, rebuild/keep-together and pruning; adopt VLC's playlist on first start
- [ ] Front end: in-block reordering, block ✕ and item ✕ on entry ids, pending/failed rows
- [ ] Full TESTING.md run, then merge

## Inbox
- [x] Queue a YouTube playlist, not just single videos: stage the downloads one at a time and cap how many videos are pulled from a playlist *(idea 2026-10-23)*
- [x] Group bulk-queued items (a playlist, a TV season) in a container on the queue so the whole group can be dragged around while keeping its order *(idea 2026-10-23)*
- [x] Log everything queued over the LAN weekend by the requester's IP (no login), tie the IP to the optional name when one is given, so we can see who did the most to keep the queue going *(idea 2026-10-23)* *(2026-10-23: private JSONL log with title, category, season/episode, device, address; tools/queue_report.py for the after-party review; nothing shown on the page by choice)*
- [x] YouTube: after queueing one video from a pasted playlist, keep the playlist's results on screen so a few more can be picked; the search box is cleared after every queue now, so either stop clearing it for playlist/link searches or detect generic vs specific searches *(idea 2026-10-24)* *(done 2026-10-25: playlist results stay, queued ones tagged ✓; word searches still clear)*
- [ ] Full UAT needed: a dry run hit a lot of playback issues with the YouTube Mix / playlist feature. Symptoms: a Mix sometimes queues as single songs on their own; removing a Mix re-queues older, already-removed Mixes; a single song from a Mix search can't be queued solo; moving or updating an item deletes and randomly rebuilds a different Mix. Decision: rebuild the Mix/playlist feature from scratch so every case is covered. **Run /grilling on the design before writing code.** Likely root of the current mess: block membership keyed by file path, so cached files shared between Mixes and solo queues collide, and every rebuild of VLC's queue re-adds by path *(idea 2026-10-26)*
- [ ] Audio track setting isn't kept between Mix items and some songs: it jumps to Disabled. YouTube videos and playlist items should never offer anything but track 1 (single-track files; the track watcher and memory should leave them alone or force track 1) *(idea 2026-10-26)*


## MVP (done)
- [x] FastAPI app + phone-friendly page, LAN-reachable, VLC HTTP control
- [x] Library scan of NAS folders, grouped by title, posters from TMDB
- [x] Queue, remove, drag-to-reorder (touch friendly), queue runtime total
- [x] Seasons: collapsible, Queue season / Queue all, extras separated
- [x] Audio/subtitle switcher (custom VLC Lua request) + remembered per show
- [x] Optional name on queue requests, logged to cache/queue.log
- [x] Per-library Sync button

## Ideas
- [ ] Full UAT pass through TESTING.md (after `start.ps1`, from a PC and a phone) before the party; log anything broken here
- [ ] Parked: a pytest suite from the fake-VLC harness (reorder, blocks, solo items, episode labels, first roll, report) if the UAT finds more regressions
- [ ] Parked: offline fast path for a cached YouTube video (skip yt-dlp when the file and its remembered metadata exist) if the venue's internet is flaky
- [ ] Undecided: desktop two-column layout above ~1400px (Now playing + Up next left, library right) so the queue stays in view while browsing
- [x] Secret menu: `@claude` in the search box rolls a random movie / episode / anime / cached YouTube video (2026-10-23)
- [ ] `/qr` page with a QR code for the LAN address *(2026-10-23: do after the `.lan` record is in place; then it encodes the readable address and gets printed for the wall, mainly for phones and Android with Private DNS)*
- [ ] Readable address for the party: a local DNS record at the gateway router (e.g. `queue.lan` -> this PC's reserved IP), the same plan as 3D_flex docs/deployment.md §5; then the README's short-address section and the QR idea become one line
- [x] Show own filename for queue items (done server-side; verify after restart) *(2026-10-23: the file line was right; the title line now adds an episode marker, S02E05 / E02 / the file's own name for extras, so a queued season no longer reads as 24 rows of the show title. Now playing shows it too.)*

## YouTube
- [x] Queue a whole playlist (first `YT_PLAYLIST_MAX`, downloaded one at a time, kept together as a block) — 2026-10-23
- [x] Research: streaming YouTube's separate audio into VLC gives no sound; downloading + merging does work
- [x] YouTube chip: search or pasted link (from any library), thumbnails, Queue
- [x] Background download with progress in Up next, cached in cache/youtube, auto-queued when done
- [x] Cancel a running download from Up next
- [ ] Try it at a party: watch for yt-dlp breakage (start.ps1 updates it) and long-video download times
- [x] Cap quality at 720p: done 2026-10-23 for playlist videos and anything YouTube files under Music (not by length, which was the first idea)

## Images
- [x] Drop/pick an image, choose display time, queued like a video (VLC shows stills natively)

## Music
- [ ] NAS music library (add a Music folder + audio extensions) if ever wanted
- [ ] Internet radio presets (trivial: VLC plays stream URLs directly)
- [ ] Spotify / YouTube Music / Apple Music: not feasible through VLC (DRM); would need a separate player integration
