# Backlog

## Inbox
- [x] Queue a YouTube playlist, not just single videos: stage the downloads one at a time and cap how many videos are pulled from a playlist *(idea 2026-10-23)*
- [x] Group bulk-queued items (a playlist, a TV season) in a container on the queue so the whole group can be dragged around while keeping its order *(idea 2026-10-23)*
- [x] Log everything queued over the LAN weekend by the requester's IP (no login), tie the IP to the optional name when one is given, so we can see who did the most to keep the queue going *(idea 2026-10-23)* *(2026-10-23: private JSONL log with title, category, season/episode, device, address; tools/queue_report.py for the after-party review; nothing shown on the page by choice)*


## MVP (done)
- [x] FastAPI app + phone-friendly page, LAN-reachable, VLC HTTP control
- [x] Library scan of NAS folders, grouped by title, posters from TMDB
- [x] Queue, remove, drag-to-reorder (touch friendly), queue runtime total
- [x] Seasons: collapsible, Queue season / Queue all, extras separated
- [x] Audio/subtitle switcher (custom VLC Lua request) + remembered per show
- [x] Optional name on queue requests, logged to cache/queue.log
- [x] Per-library Sync button

## Ideas
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
- [ ] Optional: cap quality at 720p for videos over 30 min to shorten the wait

## Images
- [x] Drop/pick an image, choose display time, queued like a video (VLC shows stills natively)

## Music
- [ ] NAS music library (add a Music folder + audio extensions) if ever wanted
- [ ] Internet radio presets (trivial: VLC plays stream URLs directly)
- [ ] Spotify / YouTube Music / Apple Music: not feasible through VLC (DRM); would need a separate player integration
