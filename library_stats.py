#!/usr/bin/env python3
"""
A one-screen health check for your library: how much cover art, how many artist
pictures, how much lyrics coverage, and whether any tags look clearly wrong - the
same things `--list-missing` on each tool already tells you, gathered into one view.

Read-only and offline: nothing is looked up online and nothing is written, so this
is always safe and fast to run. Each section is scoped to that tool's own configured
folders (which may be a subset of your library, e.g. if album_art.folders only
covers a couple of download folders) - the tag check has no folders setting of its
own, so it always covers the whole library, and says so.

Usage:
  python3 library_stats.py              # one report, using each tool's own configured folders
  python3 library_stats.py --no-progress
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from common import (STOP, dim, find_audio, fit, folder_problem, heading, install_stop_handler, load_config, log,
                    progress, red, require, resolve, start_log)

require("mutagen")

from fix_album_art import AUDIO_EXTS, UNSUPPORTED_EXTS, has_art
from find_artist_art import collect as collect_artists
from find_artist_art import has_picture, plural
from find_lyrics import has_lyrics, load_checked
from fix_misidentified_tags import SUPPORTED_EXTENSIONS, process_audio_file


def fact(label: str, value) -> None:
    log(f"  {dim(fit(label, 11))} {value}")


def _scoped_files(cfg: dict, section: str, exts: set, label: str):
    """Files in this section's configured folders, or None (after logging why) if a folder can't be read."""
    folders = [resolve(f, cfg["music_dir"]) for f in cfg[section]["folders"]]
    for f in folders:
        problem = folder_problem(f)
        if problem:
            log(f"  {red('✗')} {label}: can't read {f} ({problem})")
            return None, folders
    return list(find_audio(folders, exts)), folders


def art_stats(cfg: dict, workers: int, quiet: bool) -> None:
    files, folders = _scoped_files(cfg, "album_art", AUDIO_EXTS | UNSUPPORTED_EXTS, "Cover art")
    if files is None:
        return
    capable = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
    with ThreadPoolExecutor(workers) as pool:
        flags = list(progress(pool.map(has_art, capable), len(capable), "Checking art", quiet))
    have = sum(flags)
    extra = f"  ·  {len(files) - len(capable)} can't hold art (webm/wav/aac)" if len(files) > len(capable) else ""
    fact("Cover art", f"{have}/{len(capable)} have art  ({plural(len(folders), 'folder')} configured){extra}")


def artist_stats(cfg: dict, workers: int, quiet: bool) -> None:
    files, folders = _scoped_files(cfg, "artist_art", AUDIO_EXTS | UNSUPPORTED_EXTS, "Artist pics")
    if files is None:
        return
    jobs = collect_artists(files, workers, quiet)
    out_dir = resolve(cfg["artist_art"]["output_dir"], cfg["music_dir"])
    have = sum(has_picture(out_dir, j.name) for j in jobs)
    fact("Artist pics", f"{have}/{len(jobs)} artists have a picture")


def lyrics_stats(cfg: dict, workers: int, quiet: bool) -> None:
    files, folders = _scoped_files(cfg, "lyrics", AUDIO_EXTS, "Lyrics")
    if files is None:
        return
    checked = load_checked()
    formats = cfg["lyrics"]["formats"]
    saved = english = notfound = pending = 0
    for p in files:
        status = (checked.get(str(p)) or {}).get("status")
        if not has_lyrics(p, formats, checked):
            pending += 1
        elif status == "english":
            english += 1
        elif status == "notfound":
            notfound += 1
        else:
            saved += 1
    fact("Lyrics", f"{saved} saved  ·  {english} English (skipped)  ·  {notfound} not on lrclib  ·  {pending} pending")


def tag_stats(cfg: dict, workers: int, quiet: bool) -> None:
    problem = folder_problem(cfg["music_dir"])
    if problem:
        log(f"  {red('✗')} Tag check: can't read {cfg['music_dir']} ({problem})")
        return
    opts = {**cfg["tags"], "dry_run": True, "only_severe": True, "only_mismatched": False, "filter": None}
    files = list(find_audio([cfg["music_dir"]], SUPPORTED_EXTENSIONS))
    with ThreadPoolExecutor(workers) as pool:
        results = list(progress(pool.map(lambda p: process_audio_file(p, opts), files), len(files), "Checking tags", quiet))
    severe = sum(r is not None for r in results)
    fact("Tag check", f"{severe} severe mismatch(es) out of {len(files)} files  "
                      + dim("(whole library — fix_misidentified_tags has no folders setting)"))


SECTIONS = (art_stats, artist_stats, lyrics_stats, tag_stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel file reads")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start_log("stats", cfg)
    problem = folder_problem(cfg["music_dir"])
    if problem:
        sys.exit(f"Can't reach your music folder ({cfg['music_dir']}):\n  {problem}")
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    heading("Library stats", "read-only, no network")

    for section_fn in SECTIONS:
        if STOP.is_set():
            break
        section_fn(cfg, workers, quiet)
    print()


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
