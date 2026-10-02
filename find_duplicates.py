#!/usr/bin/env python3
"""
Find songs that are probably the same recording saved more than once - downloaded
twice into different folders, or in different formats/bitrates. Report only: nothing
is ever deleted, moved or changed. You decide what (if anything) to remove yourself.

Songs are grouped by artist and a cleaned-up title (YouTube upload noise and
deliberate variant words like "sped up"/"slowed"/"nightcore" stripped, so two
re-downloads of the same edit still match even if worded slightly differently), and
only flagged when their lengths are also close - a song and its own sped-up or
slowed edit share a title but have a different length, so they're correctly never
flagged together. Songs with no usable artist tag, or whose length can't be read,
are left out rather than guessed about.

Usage:
  python3 find_duplicates.py                  # duplicates.folders from config.toml
  python3 find_duplicates.py "/some/folder"   # just this folder
  python3 find_duplicates.py --tolerance 1.5  # how close two lengths must be (seconds)
  python3 find_duplicates.py --report dupes.txt
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from common import (STOP, bold, dim, find_audio, fit, folder_problem, heading, install_stop_handler, load_config,
                    log, progress, require, resolve, section, start_log, text_width, yellow)

require("mutagen")

from fix_album_art import AUDIO_EXTS, NOISE_RE, UNSUPPORTED_EXTS, clean_text
from find_lyrics import Job, TRAILING_NOISE, load_song_info
from lyrics_lang import norm

# "slowed + reverb" / "slowed and reverb" first, as one unit, so the connector isn't left stranded
DECORATION_RE = re.compile(
    r"\bslowed(?:\s*[+&]\s*|\s+and\s+)reverb\b"
    r"|\bsped[\s-]?up\b"
    r"|\bslowed(?:\s+down)?\b"
    r"|\breverb\b"
    r"|\bnightcore\b"
    r"|\b8d(?:\s+audio)?\b"
    r"|\bextended(?:\s+(?:mix|version|edit))?\b",
    re.IGNORECASE)
# these can mark a genuinely different recording (a remix, a live take), not just a tempo/pitch edit
# of the same one, so only strip them inside their own (parens)/[brackets] or after a trailing dash -
# never as a bare word that could appear inside an unrelated real title ("Live and Let Die")
BRACKETED_DECORATION_RE = re.compile(
    r"[\(\[][^\)\]]*\b(?:re-?mix(?:ed)?|live|acoustic|instrumental)\b[^\)\]]*[\)\]]", re.IGNORECASE)
TRAILING_DECORATION_RE = re.compile(
    r"[\s\-–—]+(?:re-?mix(?:ed)?|live|acoustic|instrumental)\s*$", re.IGNORECASE)


def tidy_title(title: str) -> str:
    """A title with YouTube upload noise and deliberate-variant decoration stripped, for grouping."""
    title = clean_text(title)
    for _ in range(3):
        title = NOISE_RE.sub(" ", title)
        title = TRAILING_NOISE.sub("", title)
        title = BRACKETED_DECORATION_RE.sub(" ", title)
        title = TRAILING_DECORATION_RE.sub("", title)
        title = DECORATION_RE.sub(" ", title)
        title = " ".join(title.split())
    return title


@dataclass
class Record:
    path: Path
    artist: str        # as tagged, for display
    title: str         # as tagged, for display
    artist_key: str     # normalized, for grouping only - never shown to the user
    title_key: str
    duration: float
    size: int
    ext: str


def build_records(files: list, workers: int, quiet: bool) -> tuple:
    """(records, no_artist_count, no_duration_count). One record per file with both an artist and a length."""
    jobs = [Job(p) for p in files]
    with ThreadPoolExecutor(workers) as pool:
        list(progress(pool.map(load_song_info, jobs), len(jobs), "Reading tags", quiet))
    records, no_artist, no_duration = [], 0, 0
    for job in jobs:
        artist_key = norm(clean_text(job.artist))
        if not artist_key:
            no_artist += 1
            continue
        if job.duration is None:
            no_duration += 1
            continue
        title_key = norm(tidy_title(job.title))
        if not title_key:
            continue
        try:
            size = job.path.stat().st_size
        except OSError:
            continue
        records.append(Record(job.path, job.artist, job.title, artist_key, title_key, job.duration, size,
                              job.path.suffix.lower().lstrip(".")))
    return records, no_artist, no_duration


def cluster_by_duration(members: list, tolerance: float) -> list:
    """Split a same-(artist,title) group into runs where each consecutive pair is within `tolerance`
    seconds, so one unrelated same-titled file at a very different length doesn't swallow the whole
    group. Only runs of 2 or more are duplicates; everything else is left alone."""
    ordered = sorted(members, key=lambda r: r.duration)
    clusters, current = [], [ordered[0]]
    for r in ordered[1:]:
        if r.duration - current[-1].duration <= tolerance:
            current.append(r)
        else:
            clusters.append(current)
            current = [r]
    clusters.append(current)
    return [c for c in clusters if len(c) >= 2]


def find_groups(records: list, tolerance: float) -> list:
    by_key = {}
    for r in records:
        by_key.setdefault((r.artist_key, r.title_key), []).append(r)
    groups = []
    for members in by_key.values():
        if len(members) >= 2:
            groups.extend(cluster_by_duration(members, tolerance))
    groups.sort(key=lambda g: -sum(m.size for m in g))
    return groups


def mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def report(groups: list, no_artist: int, no_duration: int, total_files: int, report_file: str = None) -> None:
    if groups:
        section("Likely duplicates")
        width = min(60, max(text_width(str(m.path)) for g in groups for m in g))
        for g in groups:
            # the group's members may be tagged slightly differently; show the first alphabetically
            rep = min(g, key=lambda m: str(m.path))   # every record here has a non-empty artist (build_records filters otherwise)
            copies = "1 copy" if len(g) == 1 else f"{len(g)} copies"
            print(f"\n  {bold(copies)}  {dim('·')}  {rep.artist}  {dim('—')}  {rep.title}")
            for m in sorted(g, key=lambda m: str(m.path)):
                print(f"    {fit(str(m.path), width)}  {mmss(m.duration):>5}  {human_size(m.size):>8}  {m.ext}")
        print(dim("\n  Review these yourself - nothing was changed or deleted."))

    freed = sum(sum(m.size for m in g) - max(m.size for m in g) for g in groups)
    print()
    print("  " + dim("─" * 46))
    print("  " + "   ".join(filter(None, [
        (yellow if groups else dim)(f"• {len(groups)} group(s), {sum(len(g) for g in groups)} file(s)"),
        dim(f"~{human_size(freed)} if you kept one copy per group") if groups else "",
    ])))
    if no_artist or no_duration:
        print(dim(f"  {no_artist} file(s) skipped (no artist could be determined), "
                  f"{no_duration} file(s) skipped (length couldn't be read) - out of {total_files} checked"))
    print()
    if report_file:
        try:
            text = "\n".join(
                "\n".join(f"{m.path}\t{mmss(m.duration)}\t{human_size(m.size)}\t{m.ext}"
                         for m in sorted(g, key=lambda m: str(m.path)))
                for g in groups) + ("\n" if groups else "")
            Path(report_file).expanduser().write_text(text, encoding="utf-8")
            print(dim(f"  Saved the group listing to {report_file}"))
        except OSError as e:
            print(dim(f"  ! couldn't write {report_file}: {e.strerror or e}"))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/folders to check (default: duplicates.folders from the config)")
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--tolerance", type=float, help="seconds two lengths can differ by and still count as the same recording")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel tag reads")
    ap.add_argument("--report", metavar="FILE", help="save the group listing as a plain text file")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start_log("duplicates", cfg)
    opts = dict(cfg["duplicates"])
    if args.tolerance is not None:
        if not 0 <= args.tolerance <= 30:
            sys.exit("--tolerance must be between 0 and 30")
        opts["tolerance_seconds"] = args.tolerance
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    heading("Duplicate songs", "report only — nothing is ever deleted")

    paths = args.paths or [resolve(f, cfg["music_dir"]) for f in opts["folders"]]
    for p in paths:
        problem = folder_problem(p) if not Path(p).expanduser().is_file() else ""
        if problem:
            sys.exit(f"Can't use {p}:\n  {problem}")
    files = list(find_audio(paths, AUDIO_EXTS | UNSUPPORTED_EXTS))
    log(f"  {len(files)} songs in {plural(len(paths), 'folder')}")
    if not files:
        print(f"\n  {yellow('•')} No songs found there.\n")
        return

    records, no_artist, no_duration = build_records(files, workers, quiet)
    if STOP.is_set():
        print(f"\n  {yellow('•')} Stopped.\n")
        return
    groups = find_groups(records, opts["tolerance_seconds"])
    report(groups, no_artist, no_duration, len(files), args.report)


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
