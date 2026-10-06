#!/usr/bin/env python3
"""
run_all.py

Run the entire music cleanup pipeline in one go, in the order that makes each step cheaper:
  1. Tidy folders: delete junk, move .lrc.bak files away, merge duplicate album folders
     (only when the library actually has some; whole library only, so skipped when you name folders)
  2. (Optional) Tags: fix misidentified tags from filenames (--tags)
  3. (Optional) Duplicate songs: remove the loose copies of songs that also sit in an album folder
     (--delete-strays; moved to the Trash on a Mac, deleted elsewhere)
  4. Album art: add missing cover art from YouTube
  5. Artist pictures: download artist photos from Deezer / YouTube
  6. Lyrics: fetch, romanize, and translate lyrics from lrclib.net
  7. (Optional) Organize: sort songs into Artist/Album/ folders for Jellyfin (--organize)

Features:
  - Typed on its own in a terminal, it first looks at your library (read-only), tells you what it found for each
    step and lets you tick the ones you want, with Preview and Apply boxes at the end. Any option on the command
    line, or no terminal (scripts, cron), skips those questions and runs exactly what you asked for.
  - Shows the plan and asks once before changing anything (--yes skips the question).
  - Hands-free by default (--auto on art tools so it never hangs waiting for user input).
  - Dry run mode (--dry-run) previews all steps without modifying files.
  - Clean Ctrl-C handling stops between steps safely.
  - Final summary showing the outcome of each step; the tidy step saves an undo file.

Usage:
  python3 run_all.py                      # tidy + art + artists + lyrics
  python3 run_all.py --organize           # ... + folder organization
  python3 run_all.py --tags --organize    # tags + everything above + organize
  python3 run_all.py --dry-run            # preview everything
  python3 run_all.py --yes                # no question asked (for scripts and cron)
  python3 run_all.py --delete-strays      # also remove loose copies of songs that are in an album folder
  python3 run_all.py "YouTube"            # run on a specific folder
"""

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import List, Tuple

from common import (HERE, bold, cyan, dim, folder_problem, green, heading, load_config, log, plural,
                    red, yellow)
from interactive import run_tool


def tidy_work(cfg: dict) -> List[str]:
    """What the folder-tidy step would do in this library (empty when there is nothing, or it can't be read)."""
    if folder_problem(cfg["music_dir"]):
        return []
    import library_layout   # folder names only: no packages needed
    try:
        s = library_layout.scan(cfg["music_dir"], library_layout.skip_folders(cfg))
    except OSError:
        return []
    moves_backups = bool(cfg["lyrics"]["backup_dir"])
    found = [(len(s.albums), "duplicate album folder"), (len(s.junk), "junk file"),
             (len(s.backups) if moves_backups else 0, ".lrc.bak file"), (len(s.empty), "empty folder")]
    return [plural(n, w) for n, w in found if n]


# Steps that delete, rewrite or move files you already have get their own yes or no (art, artist pictures and
# lyrics only add files). tool -> (what it does to your files, the question, what can be undone)
CHANGES = {
    "layout": ("merges duplicate album folders and deletes junk files",
               "Really tidy the folders? An undo file is saved (deleted junk can't come back)."),
    "tags": ("rewrites the tags inside songs whose tags look wrong",
             "Really rewrite those tags? This changes the files themselves and can't be undone."),
    "duplicates": ("removes the loose copies of songs that are also in an album folder",
                   "Really remove the loose copies? They go to the Trash." if sys.platform == "darwin"
                   else "Really delete the loose copies? They can't be brought back."),
    "organize": ("moves and renames songs into Artist/Album folders",
                 "Really move the songs into Artist/Album folders? This can't be undone automatically."),
}


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")


def wants_chooser(args, parser) -> bool:
    """`mt auto` typed on its own in a terminal: ask what to do. Any option, or no terminal, means just do it."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    given = {k: v for k, v in vars(args).items() if k != "config"}
    plain = {k: v for k, v in vars(parser.parse_args([])).items() if k != "config"}
    return given == plain


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="run_all.py",
        description="Run music-tools pipeline (art, artists, lyrics, and optional organization) in one go.",
    )
    parser.add_argument("paths", nargs="*", help="Folders or files to process (default: configured folders)")
    parser.add_argument("--dry-run", action="store_true", help="Preview all steps without writing anything")
    parser.add_argument("--yes", action="store_true", help="Don't ask before changing files")
    parser.add_argument("--no-layout", action="store_true", help="Skip tidying junk and duplicate album folders")
    parser.add_argument("--organize", action="store_true", help="Organize files into Artist/Album folders for Jellyfin")
    parser.add_argument("--tags", action="store_true", help="Rebuild misidentified tags from filenames first")
    parser.add_argument("--delete-strays", action="store_true",
                        help="Remove loose copies of songs that also sit in an Artist/Album folder (Trash on a Mac)")
    parser.add_argument("--no-art", action="store_true", help="Skip album art")
    parser.add_argument("--no-artists", action="store_true", help="Skip artist pictures")
    parser.add_argument("--no-lyrics", action="store_true", help="Skip lyrics")
    parser.add_argument("--interactive", action="store_true", help="Ask to confirm unsure matches (off by default)")
    parser.add_argument("--no-auto-album", action="store_true", help="Do not search online for missing album names")
    parser.add_argument("--config", help="Path to config.toml")
    args = parser.parse_args(argv)

    extra_config = ["--config", str(args.config)] if args.config else []
    extra_paths = list(args.paths)
    work = None   # what the tidy step would do, once looked at

    if wants_chooser(args, parser):
        from auto_choose import choose
        cfg = load_config(args.config)
        work = tidy_work(cfg)
        try:
            if not choose(cfg, args, max(1, min(64, cfg["workers"])), work):
                log(yellow("\n  Nothing was changed.\n"))
                return 1
        except KeyboardInterrupt:
            log(yellow("\n  Stopped. Nothing was changed.\n"))
            return 130

    steps: List[Tuple[str, str, List[str]]] = []

    if not (args.no_layout or extra_paths):   # tidying looks at the whole library, so naming folders skips it
        if work is None:
            work = tidy_work(load_config(args.config))
        if work:
            tidy_args = [*extra_config, "--clean", "--merge-albums"]
            if not args.dry_run:
                tidy_args += ["--apply", "--yes"]   # the one question below covers it
            steps.append(("layout", "Tidy folders: " + ", ".join(work), tidy_args))

    if args.tags:
        tag_args = [*extra_config]
        if not args.dry_run:
            tag_args.append("--apply")
        tag_args.append("--only-severe")
        tag_args.extend(extra_paths)
        steps.append(("tags", "Fix misidentified tags from filenames", tag_args))

    if args.delete_strays:   # after the folders are tidy and the tags right, before anything is fetched for them
        stray_args = [*extra_config, "--delete-strays"]
        if not args.dry_run:
            stray_args += ["--apply", "--yes"]   # the one question below covers it
        stray_args.extend(extra_paths)
        steps.append(("duplicates", "Remove loose copies of songs that are also in an album folder", stray_args))

    if not args.no_art:
        art_args = [*extra_config]
        if not args.interactive:
            art_args.append("--auto")
        if args.dry_run:
            art_args.append("--dry-run")
        art_args.extend(extra_paths)
        steps.append(("art", "Find and add missing album cover art", art_args))

    if not args.no_artists:
        artist_args = [*extra_config]
        if not args.interactive:
            artist_args.append("--auto")
        if args.dry_run:
            artist_args.append("--dry-run")
        artist_args.extend(extra_paths)
        steps.append(("artists", "Find artist pictures from Deezer & YouTube", artist_args))

    if not args.no_lyrics:
        lyrics_args = [*extra_config]
        if args.dry_run:
            lyrics_args.append("--dry-run")
        lyrics_args.extend(extra_paths)
        steps.append(("lyrics", "Find, romanize, and translate lyrics", lyrics_args))

    if args.organize:
        org_args = [*extra_config]
        if args.dry_run:
            org_args.append("--dry-run")
        if args.no_auto_album:
            org_args.append("--no-auto-album")
        org_args.extend(extra_paths)
        steps.append(("organize", "Organize into Artist/Album folders for Jellyfin", org_args))

    if not steps:
        log("No steps selected to run.")
        return 0

    heading("music-tools · All-in-one pipeline")
    if args.dry_run:
        log(yellow("Preview only (dry run): no files will be changed.\n"))

    log("Steps to execute:")
    for i, (tool_id, title, _) in enumerate(steps, 1):
        log(f"  {bold(str(i))}. {cyan(title)}")
    log("")

    if not (args.dry_run or args.yes):
        risky = [step for step in steps if step[0] in CHANGES]
        if risky:
            log("These steps delete, rewrite or move files you already have. Say yes or no to each one:\n")
            for step in risky:
                tool_id, title, _ = step
                log(f"  {bold(title)}\n  {dim(CHANGES[tool_id][0])}")
                if not confirm(CHANGES[tool_id][1]):
                    steps.remove(step)
                    log(yellow("  Skipped.\n"))
                else:
                    log("")
            if not steps:
                log(yellow("  Nothing was changed.\n"))
                return 1
            log("Running: " + ", ".join(cyan(title) for _, title, _ in steps) + "\n")
        elif not confirm("Run these steps and change your files?"):
            log(yellow("\n  Nothing was changed. Add --yes to run without asking, or --dry-run to preview.\n"))
            return 1

    results = []
    for i, (tool_id, title, tool_args) in enumerate(steps, 1):
        log("\n" + "=" * 60)
        heading(f"Step {i}/{len(steps)}: {title}")
        log("=" * 60 + "\n")
        code = run_tool(tool_id, tool_args)
        results.append((title, code))
        if code in (130, 2):  # 130 = Ctrl-C (SIGINT), 2 = argument/usage error
            log(yellow(f"\nStep '{title}' cancelled or syntax error (exit {code}). Aborting pipeline.\n"))
            break
        elif code not in (0, None):
            log(yellow(f"\nStep '{title}' completed with warnings or unresolved items (exit {code}). Continuing pipeline...\n"))

    log("\n" + bold("All-in-one pipeline summary:"))
    all_ok = True
    for title, code in results:
        if code in (0, None):
            status = green("✓ Done")
        elif code == 1:
            status = yellow("~ Done (with warnings/notices)")
        else:
            status = red(f"✗ Failed (exit {code})")
            all_ok = False
        log(f"  {status}  {title}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
