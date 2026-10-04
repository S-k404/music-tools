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
import os
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import (STOP, dim, find_audio, fit, folder_problem, heading, install_stop_handler, load_config, log,
                    plural, progress, red, require, resolve, start_log)

require("mutagen")

import mutagen

from fix_album_art import AUDIO_EXTS, UNSUPPORTED_EXTS, has_art
from find_artist_art import collect as collect_artists
from find_artist_art import artist_homes, has_picture
from find_lyrics import has_lyrics, load_checked
from fix_misidentified_tags import SUPPORTED_EXTENSIONS, get_current_tags, is_severe_mismatch, process_audio_file


def fact(label: str, value) -> None:
    log(f"  {dim(fit(label, 11))} {value}")


ALL_EXTS = AUDIO_EXTS | UNSUPPORTED_EXTS | SUPPORTED_EXTENSIONS


class Library:
    """The music folder, walked once for the whole report instead of once per section. A section's
    configured folders usually sit inside it, so their files are picked out of this one listing."""

    def __init__(self, music_dir):
        self.root = Path(music_dir).expanduser()
        self._files = None

    def _all(self) -> list:
        if self._files is None:
            self._files = list(find_audio([self.root], ALL_EXTS))
        return self._files

    def _inside(self, folder: Path) -> bool:
        """Whether this folder's files are all in the shared listing (same path, no symlinks in between)."""
        if folder == self.root:
            return folder.is_dir()
        try:
            rel = folder.relative_to(self.root)
        except ValueError:
            return False
        return folder.is_dir() and os.path.realpath(folder) == os.path.join(os.path.realpath(self.root), rel)

    def files(self, folders: list, exts: set) -> list:
        out = []
        for folder in folders:
            if self._inside(folder):
                out += [p for p in self._all() if p.suffix.lower() in exts and p.is_relative_to(folder)]
            else:
                out += find_audio([folder], exts)
        return out


def _scoped_files(cfg: dict, section: str, exts: set, label: str, library: Library):
    """Files in this section's configured folders, or None (after logging why) if a folder can't be read."""
    folders = [resolve(f, cfg["music_dir"]) for f in cfg[section]["folders"]]
    for f in folders:
        problem = folder_problem(f)
        if problem:
            log(f"  {red('✗')} {label}: can't read {f} ({problem})")
            return None, folders
    return library.files(folders, exts), folders


def art_stats(cfg: dict, workers: int, quiet: bool, library: Library) -> None:
    files, folders = _scoped_files(cfg, "album_art", AUDIO_EXTS | UNSUPPORTED_EXTS, "Cover art", library)
    if files is None:
        return
    capable = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
    with ThreadPoolExecutor(workers) as pool:
        flags = list(progress(pool.map(has_art, capable), len(capable), "Checking art", quiet))
    have = sum(flags)
    extra = f"  ·  {len(files) - len(capable)} can't hold art (webm/wav/aac)" if len(files) > len(capable) else ""
    fact("Cover art", f"{have}/{len(capable)} have art  ({plural(len(folders), 'folder')} configured){extra}")


def artist_stats(cfg: dict, workers: int, quiet: bool, library: Library) -> None:
    files, folders = _scoped_files(cfg, "artist_art", AUDIO_EXTS | UNSUPPORTED_EXTS, "Artist pics", library)
    if files is None:
        return
    jobs = collect_artists(files, workers, quiet)
    out_dir = resolve(cfg["artist_art"]["output_dir"], cfg["music_dir"])
    homes = artist_homes(cfg["music_dir"]) if cfg["artist_art"]["placement"] == "artist_folder" else None
    have = sum(has_picture(out_dir, j.name, homes) for j in jobs)
    fact("Artist pics", f"{have}/{len(jobs)} artists have a picture")


def lyrics_stats(cfg: dict, workers: int, quiet: bool, library: Library) -> None:
    files, folders = _scoped_files(cfg, "lyrics", AUDIO_EXTS, "Lyrics", library)
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


def read_tags(path: Path, opts: dict):
    """
    (severe mismatch?, missing a title or artist?) for one song, opening the file once.
    None when it isn't a readable audio file. The full tag check only runs on the rare
    severe candidates, same as `mt fix --only-severe`.
    """
    if STOP.is_set():
        return None
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return None
    if audio is None:
        return None
    tags = get_current_tags(audio)
    severe = (is_severe_mismatch(unicodedata.normalize("NFC", path.stem), tags["title"])
              and process_audio_file(path, opts) is not None)
    return severe, not (tags["title"] and tags["artist"])


def tag_stats(cfg: dict, workers: int, quiet: bool, library: Library) -> None:
    problem = folder_problem(cfg["music_dir"])
    if problem:
        log(f"  {red('✗')} Tag check: can't read {cfg['music_dir']} ({problem})")
        return
    opts = {**cfg["tags"], "dry_run": True, "only_severe": True, "only_mismatched": False, "filter": None}
    files = library.files([library.root], SUPPORTED_EXTENSIONS)
    with ThreadPoolExecutor(workers) as pool:
        results = [r for r in progress(pool.map(lambda p: read_tags(p, opts), files), len(files), "Checking tags", quiet)
                   if r is not None]
    severe = sum(r[0] for r in results)
    untagged = sum(r[1] for r in results)
    fact("Tag check", f"{severe} severe mismatch(es)  ·  {untagged} missing a title or artist  ·  out of {len(files)} files  "
                      + dim("(whole library — fix_misidentified_tags has no folders setting)"))


def layout_stats(cfg: dict, workers: int, quiet: bool, library: Library) -> None:
    import library_layout   # folder names only, so it has no packages of its own to wait for
    try:
        s = library_layout.scan(cfg["music_dir"], library_layout.skip_folders(cfg))
    except OSError as e:
        log(f"  {red('✗')} Layout: can't read {cfg['music_dir']} ({e.strerror or e})")
        return
    found = [(len(s.albums), "duplicate album folders"), (len(s.junk), "junk files"), (len(s.backups), ".lrc.bak files"),
             (len(s.empty), "empty folders")]
    text = "  ·  ".join(f"{n} {w}" for n, w in found if n)
    fact("Layout", text + "  " + dim("(mt layout has the details)") if text else "no duplicate folders or junk")


SECTIONS = (art_stats, artist_stats, lyrics_stats, tag_stats, layout_stats)


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

    library = Library(cfg["music_dir"])
    for section_fn in SECTIONS:
        if STOP.is_set():
            break
        section_fn(cfg, workers, quiet, library)
    print()


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
