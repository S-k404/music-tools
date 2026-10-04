#!/usr/bin/env python3
"""
Checks how your library is laid out on disk (Artist/Album/Track) and tidies it - folder
names only, no tags are read. By default it only REPORTS:

  - duplicate album folders under one artist ("Ado's Best" / "Ado’s Best", "Ado - Show" / "Show",
    "SUGAR RUSH" / "Sugar Rush - EP"), and artist folders spelled two ways ("A$ap Rocky" / "A$AP Rocky")
  - artist folders that look like collaborations ("A, B", "A & B", "A feat. B") - for you to review
  - junk: macOS "._" files, .DS_Store, lyrics backups (.lrc.bak) lying next to songs, empty folders,
    and odd folder names ("null", "Unknown", emoji-only), plus files loose in the library root

Nothing is moved or deleted unless you add --apply. Everything it does is saved to an undo file
(logs/layout_undo_<time>.json), and --undo --apply puts it back.

Usage:
  python3 library_layout.py                         # report only
  python3 library_layout.py --report layout.txt     # also save the full lists as a text file
  python3 library_layout.py --clean                 # preview junk cleanup (add --apply to do it)
  python3 library_layout.py --merge-albums          # preview merging duplicate album folders
  python3 library_layout.py --merge-albums --apply  # do it (asks first; --yes skips the question)
  python3 library_layout.py --undo --apply          # undo the most recent run that changed something
"""

import argparse
import filecmp
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from common import (HERE, LOG_DIR, STOP, atomic_write, dim, folder_problem, green, heading, install_stop_handler,
                    load_config, log, plural, red, resolve, section, start_log, yellow)
from lyrics_lang import norm

AUDIO = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".wav", ".aac", ".webm"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
ODD_NAMES = {"null", "none", "undefined", "unknown", "unknown album", "unknown artist", "untitled", "various"}
CHECKED_FILE = HERE / "lyrics_checked.json"   # same file the lyrics tool keeps (see find_lyrics.CHECKED_FILE)
SHOW = 12                                      # items per section printed on screen; --report has everything

_DASHES = "-‐‑‒–—―−"
SUFFIX_RE = re.compile(rf"\s*(?:[{_DASHES}]\s*(?:ep|single)|\((?:ep|single)\))\s*$", re.IGNORECASE)
COLLAB_RE = re.compile(r"\s*,\s*|\s*;\s*|\s+&\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+x\s+", re.IGNORECASE)


# ---------------------------------------------------------------- names

def is_junk(name: str) -> bool:
    return name.startswith("._") or name.lower() in JUNK_NAMES


def is_backup(name: str) -> bool:
    return name.lower().endswith(".lrc.bak") and not name.startswith("._")


def album_key(name: str, artist: str = "") -> str:
    """Folder names that mean the same album get the same key: punctuation, quote and dash styles and
    case are ignored, a leading "Artist - " (a YouTube-download habit) and a trailing "- EP" / "- Single" /
    "- Artist" are dropped. "" means the name has nothing to compare (emoji only), so it's never grouped."""
    s = unicodedata.normalize("NFKC", name).strip()
    akey = norm(artist)
    if akey:
        for sep in re.finditer(rf"\s[{_DASHES}]\s", s):
            head, tail = s[:sep.start()], s[sep.end():]
            if norm(head) == akey and norm(tail):
                s = tail
                break
        else:
            for sep in reversed(list(re.finditer(rf"\s[{_DASHES}]\s", s))):
                head, tail = s[:sep.start()], s[sep.end():]
                if norm(tail) == akey and norm(head):
                    s = head
                    break
    return norm(SUFFIX_RE.sub("", s))


def is_odd_name(name: str) -> bool:
    return name.strip().casefold() in ODD_NAMES or not norm(name)


# ---------------------------------------------------------------- scanning

@dataclass
class Folder:
    path: Path
    audio: int = 0
    files: int = 0


@dataclass
class DupGroup:
    parent: Path
    key: str
    folders: list = field(default_factory=list)   # canonical (the one kept) first


@dataclass
class Scan:
    root: Path
    artist_dirs: list = field(default_factory=list)
    loose: list = field(default_factory=list)          # real files sitting in the library root
    junk: list = field(default_factory=list)
    backups: list = field(default_factory=list)        # .lrc.bak next to songs
    empty: list = field(default_factory=list)          # folders with nothing real in them, deepest first
    odd: list = field(default_factory=list)            # Folder, odd names
    albums: list = field(default_factory=list)         # DupGroup, duplicate album folders
    artists: list = field(default_factory=list)        # DupGroup, artist folders spelled two ways
    collabs: list = field(default_factory=list)        # Folder, "A & B"-style artist folders
    audio_total: int = 0


def _canonical(folders: list, artist: str) -> list:
    """Order duplicates best-first: most songs, then a name that has no "Artist - " prefix or "- EP" suffix,
    then the shorter name, then alphabetical."""
    def rank(f: Folder):
        name = f.path.name
        plain = album_key(name, artist) == norm(unicodedata.normalize("NFKC", name)) and not SUFFIX_RE.search(name)
        return (-f.audio, 0 if plain else 1, len(name), name)
    return sorted(folders, key=rank)


def scan(root, skip=()) -> Scan:
    """Walk the library once. `skip` are folders to leave out (artist pictures, lyrics backups...)."""
    root = Path(os.path.abspath(root))
    skip_abs = {Path(os.path.abspath(p)) for p in skip}
    out = Scan(root)
    counts = defaultdict(lambda: [0, 0])   # folder at depth 1 or 2 -> [audio files, all real files]
    nonempty, seen_dirs = set(), []
    for cur, dnames, fnames in os.walk(root):
        here = Path(cur)
        dnames[:] = sorted(d for d in dnames if not d.startswith((".", "$")) and here / d not in skip_abs)
        if here != root:
            seen_dirs.append(here)
        for f in fnames:
            p = here / f
            if is_junk(f):
                out.junk.append(p)
                continue
            if is_backup(f):
                out.backups.append(p)
                continue
            parts = p.relative_to(root).parts
            if len(parts) == 1:
                out.loose.append(p)
                continue
            audio = p.suffix.lower() in AUDIO
            for depth in (1, 2):
                if len(parts) > depth:
                    c = counts[root.joinpath(*parts[:depth])]
                    c[0] += audio
                    c[1] += 1
            out.audio_total += audio
            for anc in p.parents:
                if anc == root:
                    break
                nonempty.add(anc)
    out.empty = sorted((d for d in seen_dirs if d not in nonempty), key=lambda d: (-len(d.parts), str(d)))
    out.artist_dirs = sorted(d for d in seen_dirs if d.parent == root)

    def folder(p: Path) -> Folder:
        a, n = counts.get(p, (0, 0))
        return Folder(p, a, n)

    children = defaultdict(list)
    for d in seen_dirs:
        children[d.parent].append(d)
    by_artist_key = defaultdict(list)
    for artist in out.artist_dirs:
        if is_odd_name(artist.name):
            out.odd.append(folder(artist))
        else:
            by_artist_key[norm(artist.name)].append(artist)
            if len([p for p in COLLAB_RE.split(artist.name) if p.strip()]) > 1:
                out.collabs.append(folder(artist))
        by_key = defaultdict(list)
        for album in sorted(children[artist]):
            if is_odd_name(album.name):
                out.odd.append(folder(album))
                continue
            key = album_key(album.name, artist.name)
            if key:
                by_key[key].append(album)
        for key, dirs in by_key.items():
            if len(dirs) > 1:
                out.albums.append(DupGroup(artist, key, _canonical([folder(d) for d in dirs], artist.name)))
    for key, dirs in by_artist_key.items():
        if len(dirs) > 1:
            out.artists.append(DupGroup(root, key, _canonical([folder(d) for d in dirs], "")))
    out.albums.sort(key=lambda g: str(g.parent).casefold())
    out.artists.sort(key=lambda g: g.key)
    return out


# ---------------------------------------------------------------- report

def _rel(scan_: Scan, p: Path) -> str:
    try:
        return str(p.relative_to(scan_.root))
    except ValueError:
        return str(p)


def report_lines(s: Scan, full: bool) -> list:
    """The report as (heading, [lines]) pairs; `full` lists everything instead of the first few."""
    cap = None if full else SHOW
    out = []

    def add(title, items, fmt):
        if items:
            shown = items if cap is None else items[:cap]
            lines = [fmt(i) for i in shown]
            if len(items) > len(shown):
                lines.append(f"… and {len(items) - len(shown)} more (use --report FILE for the full list)")
            out.append((f"{title} ({len(items)})", lines))

    def group_lines(words):
        def fmt(g: DupGroup) -> str:
            keep, *rest = g.folders
            where = _rel(s, g.parent) if g.parent != s.root else "(top level)"
            text = [f"{where}"] + [f"    {words[0]}  {keep.path.name}  ({plural(keep.audio, 'song')})"]
            text += [f"    {words[1]}  {f.path.name}  ({plural(f.audio, 'song')})" for f in rest]
            return "\n".join(text)
        return fmt

    add("Duplicate album folders", s.albums, group_lines(("keep ", "merge")))
    add("Artist folders spelled two ways (review yourself; never merged automatically)", s.artists,
        group_lines(("main ", "also ")))
    add("Collaboration-style artist folders (review yourself; never merged automatically)", s.collabs,
        lambda f: f"{f.path.name}  ({plural(f.audio, 'song')})")
    add("Odd folder names", s.odd, lambda f: f"{_rel(s, f.path)}  ({plural(f.files, 'file')})")
    add("Files loose in the library root", s.loose, lambda p: p.name)
    add("macOS / system junk files", s.junk, lambda p: _rel(s, p))
    add("Lyrics backups next to songs (.lrc.bak)", s.backups, lambda p: _rel(s, p))
    add("Empty folders (nothing but junk, or nothing)", s.empty, lambda p: _rel(s, p))
    return out


def print_report(s: Scan, report_file: str = None) -> None:
    sections = report_lines(s, full=False)
    if not sections:
        print(f"\n  {green('✓')} Nothing to tidy: no duplicate folders, junk or odd names found.")
    for title, lines in sections:
        section(title)
        for line in lines:
            print("   " + line.replace("\n", "\n   "))
    problems = [(len(s.albums), "duplicate album folder", "duplicate album folders"),
                (len(s.artists), "artist folder spelled two ways", "artist folders spelled two ways"),
                (len(s.junk), "junk file", "junk files"), (len(s.backups), ".lrc.bak file", ".lrc.bak files"),
                (len(s.empty), "empty folder", "empty folders"), (len(s.odd), "odd folder name", "odd folder names"),
                (len(s.loose), "loose root file", "loose root files"),
                (len(s.collabs), "collab-style folder", "collab-style folders")]
    print("\n  " + dim("─" * 46))
    summary = "   ".join(f"{n} {one if n == 1 else many}" for n, one, many in problems if n) or "nothing to tidy"
    print("  " + (yellow if any(p[0] for p in problems) else green)(f"{len(s.artist_dirs)} artist folders, "
                                                                     f"{s.audio_total} songs  ·  {summary}"))
    print(dim("  Report only: nothing was changed. --clean, --merge-albums and --merge-artists preview fixes (--apply does them).\n"))
    if report_file:
        try:
            text = "\n\n".join(f"{t}\n" + "\n".join("  " + l.replace("\n", "\n  ") for l in lines)
                               for t, lines in report_lines(s, full=True)) + "\n"
            Path(report_file).expanduser().write_text(text, encoding="utf-8")
            print(dim(f"  Saved the full report to {report_file}\n"))
        except OSError as e:
            print(dim(f"  ! couldn't write {report_file}: {e.strerror or e}\n"))


# ---------------------------------------------------------------- doing things (always with an undo record)

class Recorder:
    """Remembers every change so --undo can reverse it. Written to logs/layout_undo_<time>.json at the end."""

    def __init__(self, root: Path):
        self.root, self.ops, self.deleted = root, [], []

    def save(self) -> Path:
        if not (self.ops or self.deleted):
            return None
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"layout_undo_{time.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        atomic_write(path, json.dumps({"root": str(self.root), "ops": self.ops, "deleted": self.deleted},
                                      ensure_ascii=False, indent=1))
        return path


def rekey_checked(mapping: dict) -> int:
    """Moved songs keep their 'already checked' answers in lyrics_checked.json: rename the old paths."""
    if not mapping or not CHECKED_FILE.is_file():
        return 0
    try:
        data = json.loads(CHECKED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    changed = 0
    for old, new in mapping.items():
        if old in data and new not in data:
            data[new] = data.pop(old)
            changed += 1
    if changed:
        atomic_write(CHECKED_FILE, json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True))
    return changed


def move_file(src: Path, dest: Path, rec: Recorder) -> str:
    """Move without ever overwriting: 'moved', 'same' (an identical copy is already there, so the source
    is dropped) or 'conflict' (a different file is there; nothing is touched)."""
    if os.path.lexists(dest):
        try:
            if src.stat().st_size == dest.stat().st_size and filecmp.cmp(src, dest, shallow=False):
                src.unlink()
                rec.ops.append({"op": "dedup", "from": str(src), "same_as": str(dest)})
                return "same"
        except OSError:
            pass
        return "conflict"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    rec.ops.append({"op": "move", "from": str(src), "to": str(dest)})
    return "moved"


def remove_empty_dirs(dirs: list) -> int:
    """rmdir each (deepest first); a folder that still has something in it is left alone."""
    removed = 0
    for d in dirs:
        try:
            d.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def backup_target(root: Path, bak: Path, backup_dir: Path) -> Path:
    """Where the lyrics tool looks for this backup when lyrics.backup_dir is set (a mirror of the library)."""
    return backup_dir / bak.relative_to(root)


def do_clean(s: Scan, backup_dir, apply: bool) -> Recorder:
    rec = Recorder(s.root)
    section("Junk cleanup" + ("" if apply else "  (preview: nothing changed)"))
    log(f"  {plural(len(s.junk), 'junk file')} to delete")
    if backup_dir:
        log(f"  {plural(len(s.backups), '.lrc.bak file')} to move into {backup_dir}")
    elif s.backups:
        log(f"  {plural(len(s.backups), '.lrc.bak file')} left where they are: set lyrics.backup_dir first "
            f"(mt set lyrics.backup_dir \"/some/folder\") so --restore-lrc can still find them")
    if not apply:
        for p in s.junk[:SHOW]:
            log(f"    delete {_rel(s, p)}")
        if len(s.junk) > SHOW:
            log(f"    … and {len(s.junk) - SHOW} more")
        if backup_dir:
            for p in s.backups[:SHOW]:
                log(f"    move   {_rel(s, p)}  →  {backup_target(s.root, p, backup_dir)}")
        log(f"  {plural(len(s.empty), 'empty folder')} would be removed")
        return rec
    for p in s.junk:
        if STOP.is_set():
            break
        try:
            p.unlink()
            rec.deleted.append(str(p))
        except OSError as e:
            log(f"  {red('✗')} {_rel(s, p)}: {e.strerror or e}")
    moved = {}
    if backup_dir:
        for p in s.backups:
            if STOP.is_set():
                break
            dest = backup_target(s.root, p, backup_dir)
            try:
                result = move_file(p, dest, rec)
            except OSError as e:
                log(f"  {red('✗')} {_rel(s, p)}: {e.strerror or e}")
                continue
            if result == "conflict":
                log(f"  {yellow('•')} {_rel(s, p)}: a different backup is already at {dest}; left alone")
            elif result == "moved":
                moved[str(p)] = str(dest)
    removed = remove_empty_dirs(s.empty)
    log(f"  {green('✓')} deleted {plural(len(rec.deleted), 'junk file')}, moved {plural(len(moved), 'backup')}, "
        f"removed {plural(removed, 'empty folder')}")
    return rec


def do_merge(s: Scan, groups: list, apply: bool, rec: Recorder = None, what: str = "duplicate album") -> Recorder:
    """Move everything in each group's other folders into its first (the one kept). Works for album groups and,
    with what="artist", for artist folders spelled two ways: the files keep their path below the folder."""
    rec = rec or Recorder(s.root)
    section(f"Merging {what} folders" if apply else f"Merge preview ({what} folders): nothing changed")
    totals = Counter()
    mapping = {}
    for g in groups:
        keep, *rest = g.folders
        for fld in rest:
            moves = []
            leftovers = []
            for cur, _, fnames in os.walk(fld.path):
                for f in sorted(fnames):
                    src = Path(cur) / f
                    (leftovers if is_junk(f) else moves).append((src, keep.path / src.relative_to(fld.path)))
            if not apply:
                clash = sum(1 for _, d in moves if os.path.lexists(d))
                log(f"  {_rel(s, fld.path)}  →  {keep.path.name}   {plural(len(moves), 'file')}"
                    + (f"  ({clash} already there)" if clash else ""))
                totals["files"] += len(moves)
                continue
            for src, dest in moves:
                if STOP.is_set():
                    break
                try:
                    result = move_file(src, dest, rec)
                except OSError as e:
                    log(f"  {red('✗')} {_rel(s, src)}: {e.strerror or e}")
                    totals["errors"] += 1
                    continue
                totals[result] += 1
                if result == "moved":
                    mapping[str(src)] = str(dest)
                elif result == "conflict":
                    log(f"  {yellow('•')} kept both: {_rel(s, src)} (a different file is at {_rel(s, dest)})")
            for src, _ in leftovers:   # junk in a folder that is being merged away
                try:
                    src.unlink()
                    rec.deleted.append(str(src))
                except OSError:
                    pass
            dirs = sorted((Path(c) for c, _, _ in os.walk(fld.path)), key=lambda d: -len(d.parts))
            if remove_empty_dirs(dirs):
                totals["folders"] += 1
    if apply:
        rekey = rekey_checked(mapping)
        log(f"  {green('✓')} moved {totals['moved']}, dropped {totals['same']} identical copies, "
            f"kept {totals['conflict']} conflicting files in place, removed {plural(totals['folders'], 'folder')}"
            + (f", updated {rekey} lyrics-checked entries" if rekey else ""))
    else:
        log(f"  {plural(len(groups), 'group')}, {plural(totals['files'], 'file')} would move")
    return rec


def _remove_empty_parents(folder: Path, root: Path) -> None:
    """After a file is moved back, drop the folders the merge had created for it (stops at the first one in use)."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def do_undo(manifest: Path, apply: bool) -> None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    ops = data.get("ops", [])
    section(f"Undo of {manifest.name}" + ("" if apply else "  (preview: nothing changed)"))
    done = skipped = 0
    mapping = {}
    for op in reversed(ops):
        if op["op"] == "move":
            src, dest = Path(op["to"]), Path(op["from"])
            if src.exists() and not os.path.lexists(dest):
                if apply:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
                    mapping[str(src)] = str(dest)
                    _remove_empty_parents(src.parent, Path(data.get("root", "")))
                done += 1
            else:
                skipped += 1
        elif op["op"] == "dedup":
            src, dest = Path(op["same_as"]), Path(op["from"])
            if src.exists() and not os.path.lexists(dest):
                if apply:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                done += 1
            else:
                skipped += 1
    log(f"  {plural(done, 'file')} {'restored' if apply else 'would be restored'}"
        + (f", {plural(skipped, 'change')} skipped (already undone or changed since)" if skipped else ""))
    if data.get("deleted"):
        log(dim(f"  {plural(len(data['deleted']), 'junk file')} deleted by that run can't be brought back (they were junk)."))
    if apply:
        rekey_checked(mapping)
        manifest.rename(manifest.with_name(manifest.stem + ".undone.json"))


def latest_manifest(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    found = sorted(p for p in LOG_DIR.glob("layout_undo_*.json") if not p.name.endswith(".undone.json"))
    return found[-1] if found else None


# ---------------------------------------------------------------- main

def skip_folders(cfg: dict) -> list:
    """Folders inside the library that aren't artists: artist pictures, lyrics backups, anything in layout.ignore."""
    root = cfg["music_dir"]
    skip = [resolve(cfg["artist_art"]["output_dir"], root)]
    if cfg["lyrics"]["backup_dir"]:
        skip.append(resolve(cfg["lyrics"]["backup_dir"], root))
    skip += [Path(root) / name for name in cfg["layout"]["ignore"]]
    return skip


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"\n  {question} [y/N] ").strip().lower() in ("y", "yes")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--report", metavar="FILE", help="save the full lists as a plain text file")
    ap.add_argument("--clean", action="store_true", help="delete junk files, move .lrc.bak files to lyrics.backup_dir, remove empty folders")
    ap.add_argument("--merge-albums", action="store_true", help="merge duplicate album folders into the one with the most songs")
    ap.add_argument("--merge-artists", action="store_true", help="merge artist folders spelled two ways into the one with the most songs")
    ap.add_argument("--undo", nargs="?", const=True, metavar="FILE", help="reverse the latest run (or the given undo file)")
    ap.add_argument("--apply", action="store_true", help="really do it (without this, --clean/--merge-albums/--undo only preview)")
    ap.add_argument("--yes", action="store_true", help="don't ask before --apply")
    ap.add_argument("--no-progress", action="store_true", help="accepted for consistency with the other tools")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start_log("layout", cfg)
    problem = folder_problem(cfg["music_dir"])
    if problem:
        sys.exit(f"Can't reach your music folder ({cfg['music_dir']}):\n  {problem}")
    heading("Library layout", "report only unless you add --apply")

    if args.undo:
        manifest = latest_manifest(None if args.undo is True else args.undo)
        if not manifest or not manifest.is_file():
            sys.exit("Nothing to undo: no layout_undo file found in logs/.")
        if args.apply and not args.yes and not confirm(f"Undo {manifest.name}?"):
            sys.exit("Not undone.")
        do_undo(manifest, args.apply)
        print()
        return

    log(f"  Reading {cfg['music_dir']} …")
    s = scan(cfg["music_dir"], skip_folders(cfg))
    print_report(s, args.report)
    if not (args.clean or args.merge_albums or args.merge_artists):
        return
    if args.apply and not args.yes:
        what = " and ".join(w for w, on in (("clean up junk", args.clean), ("merge artist folders", args.merge_artists),
                                            ("merge duplicate albums", args.merge_albums)) if on)
        if not confirm(f"Really {what} in {cfg['music_dir']}? An undo file is saved."):
            sys.exit("Nothing was changed.")
    rec = None
    backup_dir = resolve(cfg["lyrics"]["backup_dir"], cfg["music_dir"]) if cfg["lyrics"]["backup_dir"] else None
    try:
        if args.clean:
            rec = do_clean(s, backup_dir, args.apply)
        if args.merge_artists and not STOP.is_set():
            if args.apply and args.clean:
                s = scan(cfg["music_dir"], skip_folders(cfg))   # folders changed during the cleanup
            rec = do_merge(s, s.artists, args.apply, rec, what="artist")
        if args.merge_albums and not STOP.is_set():
            if args.apply and (args.clean or args.merge_artists):
                s = scan(cfg["music_dir"], skip_folders(cfg))   # folders changed during the earlier steps
            rec = do_merge(s, s.albums, args.apply, rec)
    finally:
        saved = rec.save() if rec and args.apply else None
    if saved:
        print(dim(f"\n  Undo file: {saved}   (python3 library_layout.py --undo --apply)"))
    print()


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
