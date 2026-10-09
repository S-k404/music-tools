"""What `mt auto` asks before it changes anything.

It first looks at the library (read-only, no network) and says what it found for each step, ticks the steps that
have something to do and leaves the rest for you to switch on. Preview and Apply come last: preview shows what
would happen, apply skips the final yes/no question. run_all.py then runs the chosen steps in its usual order.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common import dim, folder_problem, green, heading, red, resolve
from interactive import checklist, flash


@dataclass
class Found:
    hint: str                # shown next to the step
    todo: Optional[bool]     # something to do here (None: couldn't tell)


# (key, label, what it does when nothing was looked up for it)
STEPS = (
    ("layout", "Tidy folders *", "delete junk files, merge duplicate album folders"),
    ("strays", "Remove loose duplicate songs *", "keeps the copy inside an album folder"),
    ("dedupe", "Remove lower-quality duplicate songs *", "keeps the best copy of each song"),
    ("tags", "Fix wrong tags from filenames *", "rebuild Artist and Title from 'Artist - Title'"),
    ("art", "Add missing cover art", "YouTube thumbnails embedded into the songs"),
    ("artists", "Find artist pictures", "a photo for every artist, from Deezer"),
    ("lyrics", "Find and translate lyrics", "original + romanization + English"),
    ("organize", "Sort into Artist/Album folders *", "for Jellyfin, Plex or Navidrome"),
)
# switched on when there is nothing to go by (the scan couldn't tell)
ON_BY_DEFAULT = {"layout": True, "art": True, "artists": True, "lyrics": True}
# never ticked for you, even when the scan finds work: they delete or move songs, or rewrite tags, so you opt in
ASK_FIRST = {"strays", "dedupe", "tags", "organize"}


def _count(n: int) -> str:
    return f"{n:,}"


def _duplicate_groups(cfg: dict, workers: int, quiet: bool, library) -> list:
    """Every group of copies of one song, found once for both of the duplicate steps (reading the tags is the slow part)."""
    import find_duplicates as dupes
    from fix_album_art import AUDIO_EXTS, UNSUPPORTED_EXTS
    folders = [resolve(f, cfg["music_dir"]) for f in cfg["duplicates"]["folders"]]
    records, _, _ = dupes.build_records(library.files(folders, AUDIO_EXTS | UNSUPPORTED_EXTS), workers, quiet)
    return dupes.find_groups(records, cfg["duplicates"]["tolerance_seconds"])


def _stray_finding(cfg: dict, groups: list) -> Found:
    import find_duplicates as dupes
    plans, _, _ = dupes.plan_strays(groups, Path(cfg["music_dir"]).expanduser())
    doomed = [m for p in plans for m in p.delete]
    if not doomed:
        return Found("no loose copies of songs that are in an album", False)
    where = "to the Trash" if dupes.uses_trash() else "deleted for good"
    return Found(f"{dupes.loose_copies(len(doomed))} ({dupes.human_size(sum(m.size for m in doomed))}), {where}", True)


def _dedupe_finding(groups: list) -> Found:
    import find_duplicates as dupes
    doomed = [m for g in groups for m in dupes.make_plan(g).losers]
    if not doomed:
        return Found("no song with a lower-quality copy", False)
    where = "to the Trash" if dupes.uses_trash() else "deleted for good"
    return Found(f"{dupes.copies(len(doomed))} ({dupes.human_size(sum(m.size for m in doomed))}), {where}", True)


def look(cfg: dict, workers: int, tidy: list, quiet: bool = False) -> tuple:
    """({step key: Found}, number of songs or None). A part that can't be checked is left out, never fatal."""
    found = {"layout": Found(", ".join(tidy) if tidy else "nothing to tidy", bool(tidy))}
    try:
        import library_stats as stats
        library = stats.Library(cfg["music_dir"])
    except (ImportError, SystemExit):   # the packages aren't installed: each step says so itself
        return found, None

    def art():
        have, capable, _, _ = stats.art_numbers(cfg, workers, quiet, library)
        return Found(f"{_count(capable - have)} of {_count(capable)} songs have none" if capable - have
                     else "every song has art", capable > have)

    def artists():
        have, total = stats.artist_numbers(cfg, workers, quiet, library)
        return Found(f"{_count(total - have)} of {_count(total)} artists have none" if total - have
                     else "every artist has a picture", total > have)

    def lyrics():
        _, _, _, pending, _ = stats.lyrics_numbers(cfg, workers, quiet, library)
        return Found(f"{_count(pending)} songs to check (English ones are skipped)" if pending
                     else "every song is done", pending > 0)

    scanned = []   # the duplicate groups, looked for once

    def groups():
        if not scanned:
            scanned.append(_duplicate_groups(cfg, workers, quiet, library))
        return scanned[0]

    for key, part in (("art", art), ("artists", artists), ("lyrics", lyrics),
                      ("strays", lambda: _stray_finding(cfg, groups())),
                      ("dedupe", lambda: _dedupe_finding(groups()))):
        try:
            found[key] = part()
        except (TypeError, OSError, ValueError, ImportError, SystemExit):   # None from a folder that can't be read, etc.
            pass
    try:
        songs = len(library.files([library.root], stats.ALL_EXTS))
    except OSError:
        songs = None
    return found, songs


def defaults_for(found: dict) -> dict:
    """Which steps start ticked: the ones with something to do, or (not looked at) the usual ones."""
    return {key: key not in ASK_FIRST and (found[key].todo if key in found and found[key].todo is not None
                                           else ON_BY_DEFAULT.get(key, False))
            for key, _, _ in STEPS}


def choose(cfg: dict, args, workers: int, tidy: list) -> bool:
    """Look, ask, and set `args` to what was picked. False when the user backed out (or the folder is unreachable)."""
    problem = folder_problem(cfg["music_dir"])
    if problem:
        print(red(f"Can't reach your music folder ({cfg['music_dir']}):\n  {problem}"))
        return False
    heading("music-tools · Auto", "looking at your library first; nothing is changed yet")
    found, songs = look(cfg, workers, tidy)
    ticked = defaults_for(found)
    preview = apply_now = False
    header = [f"Music folder  {cfg['music_dir']}  {green('✓')}"
              + (f"   {_count(songs)} songs" if songs is not None else ""),
              dim("Steps with something to do are ticked; Space switches one on or off."),
              dim("* changes files you already have: you are asked yes or no for each one before it runs.")]
    while True:
        options = [[label, ticked[key], found[key].hint if key in found else fallback]
                   for key, label, fallback in STEPS]
        options += [["Preview only (dry run)", preview, "show what would happen, change nothing"],
                    ["Apply without asking again", apply_now, "skip the yes/no questions"]]
        states = checklist("Auto · what should I do?", options, "Continue", header)
        if states is None:
            return False
        ticked = {key: on for (key, _, _), on in zip(STEPS, states)}
        preview, apply_now = states[-2], states[-1]
        if not (preview and apply_now):
            break
        flash(red("Pick either Preview (changes nothing) or Apply (no more questions), not both."))
    args.no_layout = not ticked["layout"]
    args.delete_strays = ticked["strays"]
    args.dedupe = ticked["dedupe"]
    args.tags = ticked["tags"]
    args.no_art = not ticked["art"]
    args.no_artists = not ticked["artists"]
    args.no_lyrics = not ticked["lyrics"]
    args.organize = ticked["organize"]
    args.dry_run = preview
    args.yes = apply_now
    return True
