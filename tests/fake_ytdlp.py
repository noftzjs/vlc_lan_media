"""A stand-in for the yt-dlp program, run as a real process by youtube.download().

It answers the two calls the downloader makes: the Music category check
(``--skip-download --print %(categories)s``) and a download, which prints the META,
PROGRESS and FILE lines the real one prints with our templates, and writes the
"merged" file. The file holds the format ladder it was asked for, so tests can see
which quality was chosen. Video ids starting with "bad" fail like an unavailable
video; urls containing "music" are in the Music category.
"""

import re
import sys
from pathlib import Path


def main(args: list[str]) -> int:
    url = args[-1]
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    vid = m.group(1) if m else "noidnoidnoi"
    if "--skip-download" in args:
        print("['Music']" if "music" in url else "['Entertainment']")
        return 0
    if vid.startswith("bad"):
        print("ERROR: [youtube] " + vid + ": Video unavailable", file=sys.stderr)
        return 1
    template = args[args.index("-o") + 1]
    fmt = args[args.index("-f") + 1]
    print(f"META\t{vid}\tSong {vid}\t212.0\tSome Band\thttps://i.ytimg.com/vi/{vid}/hq.jpg\tFalse", flush=True)
    for pct in ("  0.0%", " 50.0%", "100.0%"):
        print(f"PROGRESS\t{pct}\t00:01", flush=True)
    out = Path(template.replace("%(id)s", vid).replace("%(ext)s", "mkv"))
    out.write_text(fmt, encoding="utf-8")
    print(f"FILE\t{out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
