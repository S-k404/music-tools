#!/usr/bin/env python3
"""
run_all.py

Run the entire music cleanup pipeline in one go:
  1. (Optional) Tags: fix misidentified tags from filenames (--tags)
  2. Album art: add missing cover art from YouTube
  3. Artist pictures: download artist photos from Deezer / YouTube
  4. Lyrics: fetch, romanize, and translate lyrics from lrclib.net
  5. (Optional) Organize: sort songs into Artist/Album/ folders for Jellyfin (--organize)

Features:
  - Hands-free by default (--auto on art tools so it never hangs waiting for user input).
  - Dry run mode (--dry-run) previews all steps without modifying files.
  - Clean Ctrl-C handling stops between steps safely.
  - Final summary showing the outcome of each step.

Usage:
  python3 run_all.py                      # art + artists + lyrics
  python3 run_all.py --organize           # art + artists + lyrics + folder organization
  python3 run_all.py --tags --organize    # tags + art + artists + lyrics + organize
  python3 run_all.py --dry-run            # preview everything
  python3 run_all.py "YouTube"            # run on a specific folder
"""

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import List, Tuple

from common import (HERE, bold, cyan, dim, green, heading, load_config, log,
                    red, yellow)
from interactive import run_tool


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="run_all.py",
        description="Run music-tools pipeline (art, artists, lyrics, and optional organization) in one go.",
    )
    parser.add_argument("paths", nargs="*", help="Folders or files to process (default: configured folders)")
    parser.add_argument("--dry-run", action="store_true", help="Preview all steps without writing anything")
    parser.add_argument("--organize", action="store_true", help="Organize files into Artist/Album folders for Jellyfin")
    parser.add_argument("--tags", action="store_true", help="Rebuild misidentified tags from filenames first")
    parser.add_argument("--no-art", action="store_true", help="Skip album art")
    parser.add_argument("--no-artists", action="store_true", help="Skip artist pictures")
    parser.add_argument("--no-lyrics", action="store_true", help="Skip lyrics")
    parser.add_argument("--interactive", action="store_true", help="Ask to confirm unsure matches (off by default)")
    parser.add_argument("--no-auto-album", action="store_true", help="Do not search online for missing album names")
    parser.add_argument("--config", help="Path to config.toml")
    args = parser.parse_args(argv)

    extra_config = ["--config", str(args.config)] if args.config else []
    extra_paths = list(args.paths)

    steps: List[Tuple[str, str, List[str]]] = []

    if args.tags:
        tag_args = [*extra_config]
        if not args.dry_run:
            tag_args.append("--apply")
        tag_args.append("--only-severe")
        tag_args.extend(extra_paths)
        steps.append(("tags", "Fix misidentified tags from filenames", tag_args))

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
