"""After-party review of cache/queue_events.jsonl: who queued what, when, from which device.

    python tools/queue_report.py [--since 2026-11-04] [--until 2026-11-07] [--top 10] [--json out.json] [--markdown]

Nothing here is served by the app. Run it after the LAN, read it, decide what (if anything) to
post on Discord. --markdown prints a Discord-ready block; --json writes every table for a chart.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVENTS = Path(__file__).resolve().parent.parent / "cache" / "queue_events.jsonl"


def load(since: str | None, until: str | None) -> list[dict]:
    if not EVENTS.exists():
        sys.exit(f"No event log at {EVENTS}")
    rows = []
    with EVENTS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            day = r.get("ts", "")[:10]
            if (since and day < since) or (until and day > until):
                continue
            rows.append(r)
    return rows


def who(r: dict) -> str:
    return r.get("name") or r.get("ip") or "unknown"


def what(r: dict) -> str:
    """One line for an item, whatever kind of row it came from (a request, a playback outcome, a removal)."""
    cat = r.get("category") or r["kind"]
    title = r.get("title") or "?"
    if cat == "youtube":
        return (f"[{r['playlist']}] " if r.get("playlist") else "") + title
    if cat == "image":
        return f"image: {title}"
    ep = r.get("episode")
    if isinstance(ep, str):  # playback rows carry the marker the page shows (S02E05 / E02 / extra name)
        return f"{title} {ep}"
    if r.get("season") or ep is not None:  # request rows carry the raw season label and number
        return f"{title} " + " ".join(x for x in (r.get("season"), f"E{ep}" if ep is not None else None) if x)
    return title


REQUEST_KINDS = ("library", "youtube", "image")


def summarize(rows: list[dict], top: int) -> dict:
    playback = [r for r in rows if r["kind"] == "playback"]
    removed = [r for r in rows if r["kind"] == "remove"]
    controls = [r for r in rows if r["kind"] == "control"]
    rows = [r for r in rows if r["kind"] in REQUEST_KINDS]
    ok = [r for r in rows if not r.get("failed")]
    by_person = Counter(who(r) for r in ok)
    by_category = Counter(r.get("category", r["kind"]) for r in ok)
    by_device = Counter(r.get("device") or "unknown" for r in ok)
    by_hour = Counter(r["ts"][11:13] for r in ok if len(r.get("ts", "")) >= 13)
    by_day = Counter(r["ts"][:10] for r in ok)
    titles = Counter(r.get("title") or "?" for r in ok if r["kind"] == "library")
    shows = Counter(r.get("title") for r in ok if r["kind"] == "library" and r.get("category") in ("tv", "anime"))
    person_cat: dict[str, Counter] = defaultdict(Counter)
    for r in ok:
        person_cat[who(r)][r.get("category", r["kind"])] += 1
    blocks = Counter(r["block"] for r in ok if r.get("block"))
    playlists = Counter(r["playlist"] for r in ok if r.get("playlist"))
    started = sum(1 for r in ok if r.get("started_playback"))
    skipped = [r for r in playback if r.get("event") == "skipped"]
    finished = sum(1 for r in playback if r.get("event") == "finished")
    skipped_by = Counter(r.get("by") or "unknown" for r in skipped)
    skipped_titles = Counter(what(r) for r in skipped)
    controls_by = Counter((who(r), r.get("action")) for r in controls)
    failed = [r for r in rows if r.get("failed")]
    first_ts = min((r["ts"] for r in rows), default=None)
    last_ts = max((r["ts"] for r in rows), default=None)
    return {
        "span": {"first": first_ts, "last": last_ts, "requests": len(ok), "failed": len(failed), "started_playback": started,
                 "finished": finished, "skipped": len(skipped)},
        "skipped": [{"ts": r["ts"], "what": what(r), "queued_by": r.get("queued_by"), "watched_s": r.get("watched_s"),
                     "length_s": r.get("length_s"), "by": r.get("by"), "via": r.get("via")} for r in skipped],
        "skipped_titles": skipped_titles.most_common(top),
        "skipped_by": skipped_by.most_common(top),
        "removed": [{"ts": r["ts"], "what": what(r), "queued_by": r.get("queued_by"), "by": who(r), "was_playing": r.get("was_playing")} for r in removed],
        "controls_by": [{"who": w, "action": a, "count": n} for (w, a), n in controls_by.most_common()],
        "by_person": by_person.most_common(top),
        "by_person_category": {p: dict(c) for p, c in person_cat.items()},
        "by_category": by_category.most_common(),
        "by_device": by_device.most_common(),
        "by_day": sorted(by_day.items()),
        "by_hour": sorted(by_hour.items()),
        "top_titles": titles.most_common(top),
        "top_shows": shows.most_common(top),
        "blocks": blocks.most_common(top),
        "playlists": playlists.most_common(top),
        "failures": [{"ts": r["ts"], "who": who(r), "what": what(r), "error": r.get("error")} for r in failed],
        "people": sorted(by_person),
    }


def mmss(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def print_text(s: dict) -> None:
    sp = s["span"]
    print(f"Queue log: {sp['requests']} requests ({sp['failed']} failed), {sp['first']} -> {sp['last']}; "
          f"{sp['started_playback']} started playback straight away (VLC was idle)")
    print(f"Playback: {sp['finished']} watched to the end, {sp['skipped']} skipped")
    print("\nBy person")
    for p, n in s["by_person"]:
        cats = ", ".join(f"{c} {k}" for k, c in sorted(s["by_person_category"][p].items(), key=lambda kv: -kv[1]))
        print(f"  {n:4d}  {p}  ({cats})")
    print("\nBy category");  [print(f"  {n:4d}  {c}") for c, n in s["by_category"]]
    print("\nBy device");    [print(f"  {n:4d}  {d}") for d, n in s["by_device"]]
    print("\nBy day");       [print(f"  {n:4d}  {d}") for d, n in s["by_day"]]
    print("\nBy hour");      print("  " + "  ".join(f"{h}h:{n}" for h, n in s["by_hour"]))
    if s["top_shows"]:
        print("\nMost queued shows"); [print(f"  {n:4d}  {t}") for t, n in s["top_shows"]]
    print("\nMost queued titles"); [print(f"  {n:4d}  {t}") for t, n in s["top_titles"]]
    if s["blocks"]:
        print("\nSeasons / blocks"); [print(f"  {n:4d}  {b}") for b, n in s["blocks"]]
    if s["playlists"]:
        print("\nYouTube playlists"); [print(f"  {n:4d}  {b}") for b, n in s["playlists"]]
    if s["skipped"]:
        print("\nSkipped (watched / length, who pressed what)")
        for k in s["skipped"]:
            how = f"{k['by']} via {k['via']}" if k.get("by") else "unknown"
            qb = f", queued by {k['queued_by']}" if k.get("queued_by") else ""
            print(f"  {k['ts']}  {k['what']}  {mmss(k['watched_s'])}/{mmss(k['length_s'])}  {how}{qb}")
        print("\nMost skipped"); [print(f"  {n:4d}  {t}") for t, n in s["skipped_titles"]]
        print("\nSkipped by");   [print(f"  {n:4d}  {b}") for b, n in s["skipped_by"]]
    if s["removed"]:
        print("\nRemoved from the queue")
        for r in s["removed"]:
            print(f"  {r['ts']}  {r['what']}  by {r['by']}" + (" (while playing)" if r.get("was_playing") else "") + (f", queued by {r['queued_by']}" if r.get("queued_by") else ""))
    if s["controls_by"]:
        print("\nControls"); [print(f"  {c['count']:4d}  {c['who']}: {c['action']}") for c in s["controls_by"]]
    if s["failures"]:
        print("\nFailures")
        for f in s["failures"]:
            print(f"  {f['ts']}  {f['who']}: {f['what']} -> {f['error']}")


def print_markdown(s: dict) -> None:
    sp = s["span"]
    n = len(s["people"])
    print(f"**Projector queue, the weekend in numbers**  {sp['requests']} things queued by {n} {'person' if n == 1 else 'people'}.")
    print("```")
    print("Who kept it going")
    for i, (p, n) in enumerate(s["by_person"], 1):
        print(f"{i:2d}. {p:<24} {n:4d}")
    print()
    print("What  " + "  ".join(f"{c} {n}" for c, n in s["by_category"]))
    print("From  " + "  ".join(f"{d} {n}" for d, n in s["by_device"]))
    sp = s["span"]
    if sp["finished"] or sp["skipped"]:
        print(f"Watched to the end {sp['finished']}  ·  skipped {sp['skipped']}")
    if s["top_shows"]:
        print()
        print("Most queued shows")
        for t, n in s["top_shows"][:5]:
            print(f"    {t:<30} {n:4d}")
    print("```")


def main() -> None:
    # Titles carry any script (anime names, the middle dot in block labels); don't let a cp1252
    # console or a redirected file choke on them.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", help="first day to include, YYYY-MM-DD")
    ap.add_argument("--until", help="last day to include, YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", metavar="FILE", help="write every table as JSON")
    ap.add_argument("--markdown", action="store_true", help="print a Discord-ready block instead of the text report")
    a = ap.parse_args()
    s = summarize(load(a.since, a.until), a.top)
    if a.json:
        Path(a.json).write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {a.json}")
    if a.markdown:
        print_markdown(s)
    elif not a.json:
        print_text(s)


if __name__ == "__main__":
    main()
