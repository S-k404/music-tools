#!/usr/bin/env python3
"""
Find songs that are probably the same recording saved more than once - downloaded
twice into different folders, or in different formats/bitrates. By default this only
REPORTS: nothing is deleted, moved or changed. You decide what (if anything) to remove.

With --delete-strays it can also clear out the loose copies for you: a copy that sits
inside an Artist/Album/ folder is kept, and the same song loose in the library root or
directly in an artist folder is removed (with its lyrics/cover files): moved to the Trash
on a Mac, deleted for good elsewhere. Only groups that have both kinds are touched, and
nothing is removed without --apply.

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
  python3 find_duplicates.py --delete-strays          # preview: which loose copies would go
  python3 find_duplicates.py --delete-strays --apply  # remove them (asks first; --yes skips the question)
"""

import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from common import (LOG_DIR, STOP, atomic_write, bold, dim, find_audio, fit, folder_problem, green, heading,
                    install_stop_handler, load_config, log, plural, progress, red, require, resolve, section,
                    start_log, text_width, yellow)

require("mutagen")

from fix_album_art import AUDIO_EXTS, NOISE_RE, UNSUPPORTED_EXTS, clean_text, move_to_trash
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

LOSSLESS = {"flac", "wav"}
# files named after a song that belong to it (lyrics, their backup, a cover) and go when the song goes
SIDECAR_SUFFIXES = (".lrc", ".lrc.bak", ".html", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".nfo")


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


# ---------------------------------------------------------------- deleting the loose copies

def in_album_folder(path: Path, root: Path):
    """True when the song sits in an Artist/Album/ folder (or deeper), False when it is loose in the music
    folder itself or directly in an artist folder, None when it isn't under the music folder at all."""
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except (OSError, ValueError):
        return None
    return len(parts) >= 3


@dataclass
class StrayPlan:
    keep: list      # copies inside album folders: never touched
    delete: list    # loose copies that an album copy makes redundant
    held: list      # loose copies kept anyway: a lossless file when every album copy is lossy


def plan_strays(groups: list, root: Path) -> tuple:
    """(plans, no_album, nothing_loose). A group gets a plan only when it has a copy in an album folder to keep
    and a loose one to drop. Groups with no album copy (nothing says which to prefer) or no loose copy
    (nothing stray) are only counted: those are yours to decide."""
    plans, no_album, nothing_loose = [], 0, 0
    for g in groups:
        where = [(m, in_album_folder(m.path, root)) for m in g]
        keep = [m for m, w in where if w is True]
        loose = [m for m, w in where if w is False]
        if not loose:
            nothing_loose += 1
        elif not keep:
            no_album += 1
        else:
            kept_lossless = any(k.ext in LOSSLESS for k in keep)
            held = [m for m in loose if m.ext in LOSSLESS and not kept_lossless]
            plans.append(StrayPlan(keep, [m for m in loose if m not in held], held))
    return plans, no_album, nothing_loose


def sidecars_of(path: Path) -> list:
    """The lyrics, backup and cover files named after this song (song.lrc, song.html, song.jpg ...)."""
    return [f for f in (path.with_name(path.stem + suffix) for suffix in SIDECAR_SUFFIXES) if f.is_file()]


def copies(n: int) -> str:
    return "1 loose copy" if n == 1 else f"{n} loose copies"


def stem_still_used(path: Path) -> bool:
    """Whether another song with the same name stays (song.flac next to song.mp3), so the lyrics and cover
    named after it have to stay too."""
    try:
        return any(f != path and f.stem == path.stem and f.suffix.lower() in AUDIO_EXTS | UNSUPPORTED_EXTS
                   for f in path.parent.iterdir())
    except OSError:
        return True   # can't tell, so leave them


def shown_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return str(path)


def print_stray_plan(plans: list, root: Path, no_album: int, nothing_loose: int) -> None:
    section("Loose copies to remove")
    width = min(60, max([text_width(shown_path(m.path, root)) for p in plans for m in (*p.keep, *p.delete, *p.held)]
                        or [1]))
    for p in plans:
        rep = min((*p.keep, *p.delete, *p.held), key=lambda m: str(m.path))
        print(f"\n  {bold(rep.artist)}  {dim('—')}  {rep.title}")
        for label, paint, members in (("keep", green, p.keep), ("delete", red, p.delete), ("held", yellow, p.held)):
            for m in sorted(members, key=lambda m: str(m.path)):
                extra = len(sidecars_of(m.path)) if label == "delete" and not stem_still_used(m.path) else 0
                note = {"delete": f"  {dim('+ ' + plural(extra, 'lyrics/cover file'))}" if extra else "",
                        "held": f"  {dim('lossless, and no album copy is')}"}.get(label, "")
                print(f"    {paint(f'{label:<6}')}  {fit(shown_path(m.path, root), width)}  {mmss(m.duration):>5}  "
                      f"{human_size(m.size):>8}  {m.ext}{note}")
    doomed = [m for p in plans for m in p.delete]
    print()
    print("  " + dim("─" * 46))
    if doomed:
        print("  " + yellow(f"• {copies(len(doomed))} in {plural(sum(1 for p in plans if p.delete), 'group')} to "
                            f"{'move to the Trash' if uses_trash() else 'delete'}")
              + dim(f"   ~{human_size(sum(m.size for m in doomed))} freed"))
    else:
        print("  " + dim("• no loose copies to remove"))
    if no_album:
        print(dim(f"  {plural(no_album, 'group')} left alone: no copy of the song is in an album folder yet"))
    if nothing_loose:
        print(dim(f"  {plural(nothing_loose, 'group')} left alone: no loose copy"))


def remove_empty_parents(folder: Path, root: Path) -> None:
    root = root.resolve()
    try:
        folder = folder.resolve()
        while folder != root and root in folder.parents:
            folder.rmdir()   # fails (and stops) on a folder that still has something in it
            folder = folder.parent
    except OSError:
        pass


def uses_trash() -> bool:
    """On a Mac the loose copies go to the Trash (so they can be put back); elsewhere there's no helper."""
    return sys.platform == "darwin"


def remove_file(path: Path) -> str:
    """'' when it was removed, else why not."""
    try:
        if uses_trash():
            return "" if move_to_trash(path) else "couldn't move it to the Trash (allow your terminal to control Finder?)"
        path.unlink()
        return ""
    except (OSError, subprocess.SubprocessError) as e:
        return getattr(e, "strerror", None) or str(e)


def delete_strays(plans: list, root: Path) -> tuple:
    """Remove each planned loose copy and its lyrics/cover files. (removed (copy, kept copy) pairs, failures)."""
    removed, failed = [], 0
    for plan in plans:
        for m in plan.delete:
            if STOP.is_set():
                return removed, failed
            extras = [] if stem_still_used(m.path) else sidecars_of(m.path)
            why = remove_file(m.path)
            if why:
                log(f"  ! {m.path}: {why}")
                failed += 1
                continue
            for f in extras:
                remove_file(f)   # a lyrics/cover file that stays behind is harmless
            remove_empty_parents(m.path.parent, root)
            removed.append((m, plan.keep[0]))
    return removed, failed


def save_removed_list(removed: list) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"duplicates_removed_{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    atomic_write(path, "".join(f"{m.path}\tkept: {k.path}\n" for m, k in removed))
    return path


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"\n  {question} [y/N] ").strip().lower() in ("y", "yes")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/folders to check (default: duplicates.folders from the config)")
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--tolerance", type=float, help="seconds two lengths can differ by and still count as the same recording")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel tag reads")
    ap.add_argument("--report", metavar="FILE", help="save the group listing as a plain text file")
    ap.add_argument("--delete-strays", action="store_true",
                    help="keep the copies inside Artist/Album/ folders and delete the same songs loose in the "
                         "music folder or an artist folder (a preview unless you add --apply)")
    ap.add_argument("--apply", action="store_true", help="with --delete-strays: really delete them")
    ap.add_argument("--yes", action="store_true", help="don't ask before --apply")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only; --delete-strays already does this unless you add --apply")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()
    if args.apply and not args.delete_strays:
        ap.error("--apply goes with --delete-strays (the duplicate report on its own never changes anything)")
    if args.apply and args.dry_run:
        ap.error("pick one: --dry-run previews, --apply deletes")

    cfg = load_config(args.config)
    start_log("duplicates", cfg)
    opts = dict(cfg["duplicates"])
    if args.tolerance is not None:
        if not 0 <= args.tolerance <= 30:
            sys.exit("--tolerance must be between 0 and 30")
        opts["tolerance_seconds"] = args.tolerance
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    heading("Duplicate songs", ("removing loose copies" if args.apply else "preview: nothing is changed")
            if args.delete_strays else "report only — nothing is ever deleted")

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
    if not args.delete_strays:
        report(groups, no_artist, no_duration, len(files), args.report)
        return

    root = Path(cfg["music_dir"]).expanduser()
    plans, no_album, nothing_loose = plan_strays(groups, root)
    print_stray_plan(plans, root, no_album, nothing_loose)
    doomed = [p for p in plans if p.delete]
    if not doomed:
        print()
        return
    if not args.apply:
        print(dim("\n  Preview only: nothing was changed. Add --apply to remove the copies marked 'delete'.\n"))
        return
    count = sum(len(p.delete) for p in doomed)
    question = (f"Really move {copies(count)} to the Trash?" if uses_trash()
                else f"Really delete {copies(count)}? They can't be brought back.")
    if not args.yes and not confirm(question):
        sys.exit("Nothing was changed.")
    removed, failed = delete_strays(doomed, root)
    if removed:
        print(f"\n  {green('✓')} {'Moved' if uses_trash() else 'Deleted'} {copies(len(removed))}"
              + (" to the Trash" if uses_trash() else "") + (f", {failed} failed" if failed else "")
              + dim(f"   (list saved to {save_removed_list(removed)})"))
    elif failed:
        print(f"\n  {red('✗')} Nothing could be removed ({failed} failed)")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
