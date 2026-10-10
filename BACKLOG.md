# Backlog

## MVP (done)
- [x] FastAPI app + phone-friendly page, LAN-reachable, VLC HTTP control
- [x] Library scan of NAS folders, grouped by title, posters from TMDB
- [x] Queue, remove, drag-to-reorder (touch friendly), queue runtime total
- [x] Seasons: collapsible, Queue season / Queue all, extras separated
- [x] Audio/subtitle switcher (custom VLC Lua request) + remembered per show
- [x] Optional name on queue requests, logged to cache/queue.log
- [x] Per-library Sync button

## Ideas
- [ ] `/qr` page with a QR code for the LAN address
- [ ] Show own filename for queue items (done server-side; verify after restart)

## YouTube
- [x] Research: streaming YouTube's separate audio into VLC gives no sound; downloading + merging does work
- [x] YouTube chip: search or pasted link (from any library), thumbnails, Queue
- [x] Background download with progress in Up next, cached in cache/youtube, auto-queued when done
- [ ] Try it at a party: watch for yt-dlp breakage (start.ps1 updates it) and long-video download times
- [ ] Optional: cap quality at 720p for videos over 30 min to shorten the wait

## Images
- [x] Drop/pick an image, choose display time, queued like a video (VLC shows stills natively)

## Music
- [ ] NAS music library (add a Music folder + audio extensions) if ever wanted
- [ ] Internet radio presets (trivial: VLC plays stream URLs directly)
- [ ] Spotify / YouTube Music / Apple Music: not feasible through VLC (DRM); would need a separate player integration
