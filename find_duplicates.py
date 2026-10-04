#!/usr/bin/env python3
"""
Find songs that are probably the same recording saved more than once - downloaded twice into different
folders, or in different formats/bitrates - and, when you ask, remove the lower-quality copies.

By default this only reports: nothing is ever deleted, moved or changed. Every group shows which copy it
would keep (the best one: lossless beats lossy, then higher resolution or bitrate) and which it would remove.
  --delete          preview moving the lower-quality copies to the Trash (nothing is changed yet)
  --apply           do it: asks first (--yes skips the question), keeps the best copy of each song, moves the
                    rest and the lyrics/covers that only belong to them to the Trash (a copy's lyrics are kept
                    for the best copy when it has none), then sorts the kept songs into Artist/Album/ folders,
                    looking up the album of loose ones (--no-fix-albums skips that)

Songs are grouped by artist and a cleaned-up title (YouTube upload noise and
deliberate variant words like "sped up"/"slowed"/"nightcore" stripped, so two
re-downloads of the same edit still match even if worded slightly differently), and
only flagged when their lengths are also close - a song and its own sped-up or
slowed edit share a title but have a different length, so they're correctly never
flagged together. Songs with no usable artist tag, or whose length can't be read,
are left out rather than guessed about. A group whose copies are tagged as different
versions (one a remix, live take, instrumental, sped-up edit...) is listed but never
removed, even when the lengths match.

Usage:
  python3 find_duplicates.py                  # duplicates.folders from config.toml
  python3 find_duplicates.py "/some/folder"   # just this folder
  python3 find_duplicates.py --tolerance 1.5  # how close two lengths must be (seconds)
  python3 find_duplicates.py --report dupes.txt
  python3 find_duplicates.py --delete         # preview removing the lower-quality copies
  python3 find_duplicates.py --delete --apply # do it, then fix the albums of the kept songs
"""

import argparse
import math
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from common import (STOP, bold, dim, find_audio, fit, folder_problem, green, heading, install_stop_handler, load_config,
                    log, plural, progress, require, resolve, section, start_log, text_width, yellow)

require("mutagen")

from fix_album_art import AUDIO_EXTS, NOISE_RE, UNSUPPORTED_EXTS, clean_text, move_to_trash
from find_lyrics import Job, TRAILING_NOISE, load_song_info
from lyrics_lang import norm
from organize_music import COMMON_IMAGES, clean_empty_directories, is_placeholder

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


_MARKER_RE = re.compile(r"\b(?:re-?mix(?:ed)?|live|acoustic|instrumental|sped[\s-]?up|slowed|reverb|nightcore|8d|extended)\b",
                        re.IGNORECASE)


def variant_markers(title: str) -> frozenset:
    """Which deliberate-variant words a title carries (remix, live, sped up, slowed...), spelled one way each.
    tidy_title() strips them so re-downloads group together; copies that carry different ones ("Song" and
    "Song (Instrumental)") may still be different recordings of the same length, so they must never be removed."""
    text = clean_text(title)
    found = set()
    for pattern in (BRACKETED_DECORATION_RE, TRAILING_DECORATION_RE, DECORATION_RE):
        for m in pattern.finditer(text):
            for word in _MARKER_RE.findall(m.group(0)):
                word = re.sub(r"[\s-]", "", word.lower())
                found.add("remix" if word.startswith("remix") else word)
    return frozenset(found)


# ---------------------------------------------------------------- quality

# By file type: (name, lossless?, what one kbps of it is worth next to MP3). The last number is a deliberately
# rough rule of thumb (AAC, Vorbis and Opus sound as good as MP3 at lower bitrates); lossy files within about
# QUALITY_STEP of each other count as the same quality, so it only has to be right about clearly different ones.
FORMATS = {"flac": ("FLAC", True, 1.0), "wav": ("WAV", True, 1.0),
           "mp3": ("MP3", False, 1.0), "m4a": ("AAC", False, 1.25), "mp4": ("AAC", False, 1.25),
           "aac": ("AAC", False, 1.25), "ogg": ("Vorbis", False, 1.15), "opus": ("Opus", False, 1.5)}
QUALITY_STEP = 1.08


@dataclass(frozen=True)
class Quality:
    label: str     # for display: "FLAC 16-bit/44.1 kHz", "MP3 320 kbps"
    rank: tuple    # only compared, never shown: bigger is better


UNKNOWN_QUALITY = Quality("unknown", (0, 0, True))


def audio_quality(info, ext: str, size: int, duration: float) -> Quality:
    """How good a copy is, from the stream details mutagen read (`info`; None when it couldn't): lossless beats
    lossy, then higher resolution (lossless) or bitrate (lossy). A file that doesn't state its bitrate gets
    size / length, marked with a ~."""
    name, lossless, worth = FORMATS.get(ext, (ext.upper() or "?", False, 1.0))
    if getattr(info, "codec", "") == "alac":   # an .m4a can hold lossless audio too
        name, lossless = "ALAC", True
    if lossless:
        bits = getattr(info, "bits_per_sample", 0) or 16
        rate = getattr(info, "sample_rate", 0) or 44100
        # WAV can't hold proper tags or cover art, so the same audio as FLAC/ALAC is the better one to keep
        return Quality(f"{name} {bits}-bit/{rate / 1000:g} kHz", (1, bits * rate, name != "WAV"))
    kbps = (getattr(info, "bitrate", 0) or 0) / 1000
    guessed = not kbps
    if guessed:
        kbps = size * 8 / duration / 1000 if duration else 0
    level = round(math.log(max(kbps * worth, 1)) / math.log(QUALITY_STEP))
    return Quality(f"{name} {'~' if guessed else ''}{kbps:.0f} kbps", (0, level, True))


def album_tag(tagfile) -> str:
    """The Album tag of an already-parsed mutagen file ('' if it has none)."""
    tags = getattr(tagfile, "tags", None)
    if not tags:
        return ""
    for key in ("TALB", "\xa9alb", "album"):   # MP3, M4A, everything else
        try:
            value = tags.get(key)
            if value:
                return str(value[0] if isinstance(value, list) else value).strip()
        except Exception:
            continue
    return ""


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
    quality: Quality = UNKNOWN_QUALITY
    variants: frozenset = frozenset()   # variant_markers() of the title
    has_album: bool = False             # a real album tag (not blank, "Unknown", "YouTube" or the title)


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
        ext = job.path.suffix.lower().lstrip(".")
        album = album_tag(job.tagfile)
        has_album = bool(album) and not is_placeholder(album) \
            and album.casefold() not in ("youtube", job.artist.casefold(), job.title.casefold())
        records.append(Record(job.path, job.artist, job.title, artist_key, title_key, job.duration, size, ext,
                              audio_quality(getattr(job.tagfile, "info", None), ext, size, job.duration),
                              variant_markers(job.title), has_album))
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


def copies(n: int) -> str:
    return "1 copy" if n == 1 else f"{n} copies"


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


# ---------------------------------------------------------------- deciding what to keep

@dataclass
class Plan:
    group: list
    keeper: Record
    losers: list = field(default_factory=list)   # the lower-quality copies --delete would remove
    review: str = ""                              # why nothing in this group may be removed ("" = nothing in the way)


def choose_keeper(group: list) -> Record:
    """The copy to keep: the best quality; on a tie the one that already has an album tag, then the bigger file
    (more embedded art and tags), then the first path alphabetically."""
    ordered = sorted(group, key=lambda m: str(m.path))
    return max(ordered, key=lambda m: (m.quality.rank, m.has_album, m.size))   # max() keeps the first of equals


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def make_plan(group: list) -> Plan:
    keeper = choose_keeper(group)
    if len({m.variants for m in group}) > 1:
        return Plan(group, keeper, review="tagged as different versions (remix, live, instrumental, sped up...)")
    # two names for one file (a hard link, a link pointing at the other copy): removing one frees nothing
    losers = [m for m in group if m is not keeper and not same_file(m.path, keeper.path)]
    return Plan(group, keeper, losers)


def mark(plan: Plan, m: Record) -> str:
    """Returns "keep", "remove", or "" (a group left for review, or a second name for the copy kept, is never marked)."""
    if plan.review:
        return ""
    if m is plan.keeper:
        return "keep"
    return "remove" if any(m is x for x in plan.losers) else ""


# ---------------------------------------------------------------- removing

LYRICS_PARTS = (".lrc", ".lrc.bak", ".html", ".txt")        # what the lyrics tool writes next to a song
SIDECAR_PARTS = LYRICS_PARTS + (".jpg", ".jpeg", ".png", ".webp")   # + a song.jpg cover beside it


def own_sidecars(path: Path) -> list:
    """Files that belong to this song alone: its own name plus a lyrics/cover ending. None when another song in the
    folder has the same name (song.mp3 and song.flac share one song.lrc), because they aren't ours to touch then."""
    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return []
    stem = path.stem.casefold()
    if any(f != path and f.stem.casefold() == stem and f.suffix.lower() in AUDIO_EXTS | UNSUPPORTED_EXTS for f in siblings):
        return []
    found = [path.with_name(path.stem + part) for part in SIDECAR_PARTS]
    return [f for f in found if f.is_file() and f.name.lower() not in COMMON_IMAGES]


def remove_copy(keeper: Record, loser: Record) -> tuple:
    """Move one lower-quality copy to the Trash, with the files that belong only to it. Lyrics the copy to keep
    doesn't have yet are moved over to it instead. Returns (ok, note)."""
    if not keeper.path.is_file():
        return False, "the copy to keep is gone, so this one was left alone"
    if not loser.path.is_file():
        return False, "it's already gone"
    sidecars = own_sidecars(loser.path)
    if not move_to_trash(loser.path):
        return False, "couldn't move it to the Trash"
    notes = []
    for f in sidecars:
        part = f.name[len(loser.path.stem):]
        target = keeper.path.with_name(keeper.path.stem + part)
        if part in LYRICS_PARTS and not target.exists():
            try:
                shutil.move(str(f), str(target))
                notes.append(f"{part} moved to the copy kept")
                continue
            except OSError:
                pass
        if not move_to_trash(f):
            notes.append(f"couldn't trash {f.name}")
    return True, "; ".join(notes)


def remove_losers(plans: list, music_dir: str, clean_empty: bool) -> tuple:
    """Remove every plan's lower-quality copies. Returns (kept, removed, failed, freed): the copies kept in groups
    where something was removed, and the counts."""
    section("Moving the lower-quality copies to the Trash")
    kept, removed, failed, freed, streak = [], 0, 0, 0, 0
    parents = set()
    for plan in plans:
        did = False
        for m in plan.losers:
            if STOP.is_set() or streak >= 3:
                break
            ok, note = remove_copy(plan.keeper, m)
            if ok:
                removed, freed, streak, did = removed + 1, freed + m.size, 0, True
                parents.add(m.path.parent)
                log(f"  {green('✓')} {m.path}  {dim(f'{m.quality.label}; kept {plan.keeper.quality.label}')}"
                    + (f"  {dim('(' + note + ')')}" if note else ""))
            else:
                failed, streak = failed + 1, streak + 1
                log(f"  {yellow('!')} {m.path}  {dim(note)}")
        if did:
            kept.append(plan.keeper.path)
    if streak >= 3:
        log(dim("  Stopped after 3 failures in a row. Moving files to the Trash needs macOS."))
    if clean_empty and parents:
        clean_empty_directories(list(parents), Path(music_dir).resolve())
    return kept, removed, failed, freed


def fix_albums(kept: list, music_dir: str, config, quiet: bool) -> int:
    """Run the organizer on the songs that were kept: it files each under Artist/Album/, looking up the album
    of loose songs online. Songs outside the music folder stay where they are."""
    root = Path(music_dir).resolve()
    inside = [str(p) for p in kept if p.is_file() and Path(p).resolve().is_relative_to(root)]
    section("Fixing the albums of the songs that were kept")
    if len(inside) < len(kept):
        log(dim(f"  {plural(len(kept) - len(inside), 'song')} outside {root} stay where they are"))
    if not inside:
        return 0
    from interactive import run_tool   # the same way `mt all` runs its steps
    common = (["--config", str(config)] if config else []) + (["--no-progress"] if quiet else [])
    code = 0
    for batch in in_batches(inside):
        code = max(code, run_tool("organize", [*common, *batch]) or 0)
        if code == 130:   # Ctrl-C
            break
    return code


def in_batches(paths: list, limit: int = 60000):
    """Chunks of paths whose command line stays short, however many songs were kept."""
    batch, size = [], 0
    for path in paths:
        if batch and size + len(path) + 1 > limit:
            yield batch
            batch, size = [], 0
        batch.append(path)
        size += len(path) + 1
    if batch:
        yield batch


# ---------------------------------------------------------------- reporting

def report(plans: list, no_artist: int, no_duration: int, total_files: int, report_file: str = None) -> None:
    groups = [p.group for p in plans]
    if plans:
        section("Likely duplicates")
        width = min(60, max(text_width(str(m.path)) for g in groups for m in g))
        for plan in plans:
            g = plan.group
            # the group's members may be tagged slightly differently; show the first alphabetically
            rep = min(g, key=lambda m: str(m.path))   # every record here has a non-empty artist (build_records filters otherwise)
            print(f"\n  {bold(copies(len(g)))}  {dim('·')}  {rep.artist}  {dim('—')}  {rep.title}")
            for m in sorted(g, key=lambda m: str(m.path)):
                label = mark(plan, m)
                shown = (green if label == "keep" else yellow)(f"{label:<6}")
                print(f"    {shown}  {fit(str(m.path), width)}  {mmss(m.duration):>5}  {human_size(m.size):>8}  {m.quality.label}")
            if plan.review:
                print(dim(f"    ! {plan.review}: left alone"))

    removable = [p for p in plans if p.losers]
    freed = sum(m.size for p in removable for m in p.losers)
    review = sum(1 for p in plans if p.review)
    print()
    print("  " + dim("─" * 46))
    print("  " + "   ".join(filter(None, [
        (yellow if groups else dim)(f"• {len(groups)} group(s), {sum(len(g) for g in groups)} file(s)"),
        dim(f"~{human_size(freed)} freed by removing {copies(sum(len(p.losers) for p in removable))}") if removable else "",
        dim(f"{review} group(s) left for you to check") if review else "",
    ])))
    if no_artist or no_duration:
        print(dim(f"  {no_artist} file(s) skipped (no artist could be determined), "
                  f"{no_duration} file(s) skipped (length couldn't be read) - out of {total_files} checked"))
    print()
    if report_file:
        try:
            text = "\n".join(
                "\n".join(f"{m.path}\t{mmss(m.duration)}\t{human_size(m.size)}\t{m.ext}\t{m.quality.label}\t{mark(plan, m)}"
                         for m in sorted(plan.group, key=lambda m: str(m.path)))
                for plan in plans) + ("\n" if plans else "")
            Path(report_file).expanduser().write_text(text, encoding="utf-8")
            print(dim(f"  Saved the group listing to {report_file}"))
        except OSError as e:
            print(dim(f"  ! couldn't write {report_file}: {e.strerror or e}"))


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
    ap.add_argument("--delete", action="store_true", help="preview moving the lower-quality copy of each song to the Trash")
    ap.add_argument("--apply", action="store_true", help="really do it (without this, --delete only previews)")
    ap.add_argument("--yes", action="store_true", help="don't ask before --apply")
    ap.add_argument("--no-fix-albums", action="store_true",
                    help="after removing, don't sort the kept songs into Artist/Album/ folders")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()
    if args.apply and not args.delete:
        ap.error("--apply goes with --delete")

    cfg = load_config(args.config)
    start_log("duplicates", cfg)
    opts = dict(cfg["duplicates"])
    if args.tolerance is not None:
        if not 0 <= args.tolerance <= 30:
            sys.exit("--tolerance must be between 0 and 30")
        opts["tolerance_seconds"] = args.tolerance
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    heading("Duplicate songs", "lower-quality copies go to the Trash" if args.apply
            else "preview: nothing is changed" if args.delete else "report only: nothing is ever deleted")

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
    plans = [make_plan(g) for g in find_groups(records, opts["tolerance_seconds"])]
    report(plans, no_artist, no_duration, len(files), args.report)

    todo = [p for p in plans if p.losers]
    count = sum(len(p.losers) for p in todo)
    if not args.delete:
        if todo:
            print(dim("  Nothing was changed. --delete previews moving the copies marked \"remove\" to the Trash.\n"))
        return
    if not count:
        print(f"  {yellow('•')} Nothing to remove.\n")
        return
    if not args.apply:
        print(dim(f"  Preview only: nothing was changed. Add --apply to move the {copies(count)} marked "
                  "\"remove\" to the Trash,\n  then sort the copies kept into Artist/Album/ folders.\n"))
        return
    if not args.yes and not confirm(f"Move {copies(count)} to the Trash and keep the best copy of each song?"):
        sys.exit("Nothing was changed. (--yes skips the question.)")

    kept, removed, failed, freed = remove_losers(todo, cfg["music_dir"], cfg["organize"]["clean_empty_dirs"])
    code = 1 if failed else 0
    print(f"\n  {green('✓') if removed else yellow('•')} {copies(removed)} moved to the Trash"
          f" ({human_size(freed)})" + (f", {failed} couldn't be moved" if failed else "") + dim("  (Put Back in the Trash restores one)"))
    if kept and not args.no_fix_albums and not STOP.is_set():
        code = max(code, fix_albums(kept, cfg["music_dir"], args.config, quiet))
    print()
    return code


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
