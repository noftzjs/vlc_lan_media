# LAN Party Projector Queue

Phone-friendly web page that lets anyone on the LAN browse the NAS (with poster art),
queue videos to the VLC instance driving the projector, and drag the queue into order.

## One-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Allow the web app through Windows Firewall (run once, from an **elevated** PowerShell):

```powershell
New-NetFirewallRule -DisplayName "LAN Projector Queue" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Any
```

(`-Profile Any` because this PC's network is currently classed as *Public*. Delete the rule after the party with
`Remove-NetFirewallRule -DisplayName "LAN Projector Queue"`.)

## Running it

```powershell
.\start.ps1
```

That launches VLC with its web interface (password `Password123`, bound to localhost only)
and starts the FastAPI server on port 8000. Guests open `http://<this PC's IP>:8000`.

If VLC is already open, `start.ps1` leaves it alone; in that case VLC needs
*Preferences > Interface > Main interfaces > Web* ticked and the Lua HTTP password set.

## Sharing with guests

Guests just open a URL on their phone. Make that URL easy and stable:

- **Short address.** This PC's hostname is `Noftzjs`, so try `http://noftzjs:8000`
  (Windows, Android) or `http://noftzjs.local:8000` (iPhone, Mac). Test both from a phone
  before the party. Fall back to the IP (`http://192.168.2.84:8000`) if neither resolves.
- **Pin the IP.** The Ethernet address is DHCP-assigned and can change after a reboot.
  Reserve `192.168.2.84` for this PC's MAC address in the router's DHCP settings, or set a
  static IP on the adapter.
- **QR code on the projector.** Generate a QR code for the URL, show it on the projector
  before the first video, and tape a printed copy somewhere visible. (A built-in `/qr` page
  is on the ideas list below.)
- **Same network.** Guests must be on the main Wi-Fi, not a guest network. Guest networks
  usually isolate clients from the LAN.
- **Wired PC.** Keep the projector PC on Ethernet so playback from the NAS is smooth.

There is no login. Anyone who can reach the page controls the projector, which is fine for a
room of friends and nothing else. Never port-forward this or expose it to the internet, and
leave VLC's own web interface bound to localhost (the start script does this).

## Ideas / future features

- `/qr` page that renders a QR code for the current LAN address, plus a copy-link line in the UI.
- Add more here as they come up.

## Posters

Cover art comes from [The Movie Database](https://www.themoviedb.org) (TMDB). It's free:

1. Create an account at themoviedb.org and go to *Settings > API*.
2. Copy either the **API Key** or the **API Read Access Token**.
3. Copy `.env.example` to `.env` and paste it in as `TMDB_API_KEY=...`.
4. Restart the server.

On the first run the server fetches a poster for every title in the background (a few minutes
for ~1,400 titles) and caches them in `cache/posters/`, so later runs are instant and work
offline. Titles are parsed from folder names like `12 Strong (2018) [1080p]`; a title with no
match just shows a text placeholder. Delete `cache/posters.json` to force a fresh lookup.

Without a key everything else still works, you just get placeholders.

## Audio and subtitle tracks

The Now Playing panel shows Audio and Subtitles dropdowns whenever the playing file has more
than one track (dual-audio anime, commentary tracks, forced subs). Changing one switches VLC
live, same as pressing B / V on the keyboard.

**Picking a track remembers it for the show.** Switch a dual-audio episode to English with subs
off, and every later episode of that show (already queued or queued afterwards) starts the same
way. The choice is matched by language, so it still works when episodes have different track
layouts, and it falls back to the track's position if the language is unknown. The panel shows
"Remembered for <show>: …" with a *forget* link. Preferences live in `cache/track_prefs.json`.
One-file titles (movies) are never remembered.

How it works: VLC ignores its own `audio-language` / `sub-language` settings for many MKVs
(verified on this library, both as per-item options and as the `--audio-language` start flag),
but selecting a track by id works. So a watcher thread in the server notices each time a new
item starts playing and applies the remembered choice, usually within a second.

VLC's stock web API can't list tracks, so `vlc_http/` is a copy of VLC's own HTTP interface
folder with one extra file, `requests/tracks.json`. `start.ps1` points VLC at that folder with
`--lua-config "http={dir='...'}"`. If VLC was started some other way the dropdowns stay hidden
and the watcher does nothing.

## YouTube

Pick the **YouTube** chip and the search box searches YouTube (or takes a pasted link). Tap a
result, press Queue, and it plays on the projector like anything else: 1080p where available,
with the queue showing the video's title and thumbnail. This covers music too.

How it works: VLC 3 can't open YouTube links itself any more, and feeding it YouTube's separate
video and audio streams gives picture with no sound. So `youtube.py` runs
[yt-dlp](https://github.com/yt-dlp/yt-dlp) to **download** the video (H.264 up to 1080p plus
AAC, merged by ffmpeg into one `.mkv` in `cache/youtube/`) and queues that file. A short clip
takes a few seconds; the download shows in Up next with a percentage and joins the queue when
done. Downloads are cached in `cache/youtube/` (reused if queued again) and trimmed beyond
`YT_CACHE_GB` (20 GB, oldest first) or older than `YT_CACHE_DAYS` (7 days). Files still in
VLC's playlist are never trimmed.

yt-dlp needs a JavaScript runtime for YouTube's checks and ffmpeg for merging; `start.ps1`
downloads a portable Deno into `tools\deno` the first time and updates yt-dlp on every start,
because YouTube changes often and yt-dlp breaks until updated. ffmpeg must be on PATH (it is
on this PC).

Needs internet on the projector PC, and thumbnails are loaded from YouTube by guests' phones.
Age-restricted videos won't download (no login). At most three downloads run at once.

## Pictures on the projector

Drop an image anywhere on the page (or tap "choose one" on a phone), pick how long it should
show (10s to 5 minutes, or until skipped), and it joins the queue like a video. VLC shows
stills natively, so memes, scoreboards and "pizza is here" notes just work. JPG, PNG, GIF,
WebP and BMP up to `IMAGE_MAX_MB` (25). Uploads sit in `cache/images/` and are deleted after
`IMAGE_KEEP_DAYS` (2) unless still queued.

## Syncing the library

The NAS is scanned at startup and again whenever a library is older than `SCAN_TTL`. To pick up
changes right away, select a library chip (TV Shows, Movies, …) and press **⟳ Sync**: only that
folder is rescanned and swapped in, the others stay as they are. With *All* selected it rescans
everything. The API form is `/api/media?refresh=TV%20Shows` or `?refresh=all`.

## Seasons and "Queue season"

Opening a show lists its episodes grouped by season, each with a **Queue season** button next
to the per-episode **Queue** buttons. Shows with no season structure get a single **Queue all**.
Seasons come from `Season N` / `SxxEyy` / `01x05` patterns, or from sub-folders when the names
give nothing away. Episode numbers are parsed from the file names so the list is in watch
order, not alphabetical. Creditless openings/endings, trailers and anything in an `Extras`
folder are listed last, greyed out, and skipped by Queue season.

## Who queued what

The name box in the top bar is optional and remembered per device. Whatever is typed there is
sent with each queue request, shown as "queued by …" in the Up next list, logged to the server
console, and appended to `cache/queue.log` (timestamp, name, file).

## Reordering the queue

Drag the ⠿ handle in "Up next" (works on touch). VLC's HTTP API has no "move" command, so
the server removes the upcoming items and re-adds them in the new order; the playing item is
never touched. Already-played items stay in VLC's playlist but are hidden from the page.

## Configuration

Settings are read from `.env` (see `.env.example`) or environment variables, with defaults in `app.py`:

| Variable          | Default                          | Meaning                                              |
| ----------------- | -------------------------------- | ---------------------------------------------------- |
| `MEDIA_DIRS`      | `N:\TV Shows;N:\Movies;N:\Anime` | Semicolon-separated folders to share                 |
| `MOVIE_LIBRARIES` | `Movies`                         | Which of those folders are searched on TMDB as movies|
| `TV_LIBRARIES`    | `TV Shows`                       | ...and which as TV. Others are searched as both.     |
| `TMDB_API_KEY`    | *(empty)*                        | TMDB key for posters; empty disables them            |
| `VLC_HOST`        | `127.0.0.1`                      | Where VLC's web interface listens                    |
| `VLC_PORT`        | `8080`                           |                                                      |
| `VLC_PASSWORD`    | `Password123`                    | VLC's Lua HTTP password                              |
| `SCAN_TTL`        | `600`                            | Seconds between NAS re-scans                         |
| `YT_FORMAT`       | H.264 ≤1080p + AAC, else best    | yt-dlp format selector for YouTube downloads         |
| `YT_CACHE_GB`     | `20`                             | Trim oldest YouTube downloads beyond this size       |
| `YT_CACHE_DAYS`   | `7`                              | Delete YouTube downloads older than this             |
| `DENO_PATH`       | `tools\deno\deno.exe`            | JavaScript runtime for yt-dlp                        |
| `FFMPEG_PATH`     | ffmpeg on PATH                   | Used by yt-dlp to merge video + audio                |

## API

| Method | Path                            | What it does                                                   |
| ------ | ------------------------------- | -------------------------------------------------------------- |
| GET    | `/api/media?refresh=1`          | Titles grouped by folder, each with its files (`refresh` forces a rescan) |
| GET    | `/api/poster/{library}/{group}` | Cached poster JPEG, 404 if none                                |
| POST   | `/api/queue` `{id, user?}`      | Enqueue a file; starts playback if VLC is idle                 |
| POST   | `/api/queue/batch` `{ids, user?}` | Enqueue several in order (max 200), e.g. a season           |
| POST   | `/api/queue/image` (multipart: `file`, `user?`, `duration?`) | Upload an image and queue it for `duration` seconds (-1 = until skipped) |
| GET    | `/api/youtube/search?q=`        | YouTube search results, or the video behind a pasted link      |
| POST   | `/api/queue/youtube` `{url, user?, title?}` | Download a YouTube video in the background and queue it when done; progress appears in `/api/status` `pending` |
| GET    | `/api/tracks`                   | Audio + subtitle tracks of the playing file (501 without `vlc_http`) |
| POST   | `/api/tracks` `{kind, id}`      | Select a track and remember it for the show; `kind` is `audio` or `subtitle`, `-1` disables |
| DELETE | `/api/tracks/pref`              | Forget the remembered tracks for the playing show              |
| POST   | `/api/queue/reorder` `{ids}`    | New order for the upcoming items (409 if the queue changed)    |
| DELETE | `/api/queue/{id}`               | Remove an item from VLC's playlist                             |
| GET    | `/api/status`                   | Now playing (with title + poster), position, and the queue     |
| POST   | `/api/control` `{action}`       | `pause` `play` `stop` `next` `previous` `fullscreen`           |

Interactive docs: `http://localhost:8000/docs`.
