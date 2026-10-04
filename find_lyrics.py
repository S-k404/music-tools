#!/usr/bin/env python3
"""
Find lyrics for your songs: the original text (time-synced when there is timing),
a romanization for Japanese/Korean/Chinese, and an English translation - all
three side by side.

Lyrics come from a .lrc file you already have next to the song, the lyrics in
the song's tags, or lrclib.net (free, no account or key), matched on artist,
title and length so a song it doesn't have is reported as not found instead of
getting somebody else's lyrics. The language is worked out first: songs that
are already English aren't translated (and with --only-foreign aren't saved at all).

Saved next to the song, so music players and file browsers find them the normal way:

  <song>.lrc    synced lyrics for music players: every line is followed by its
                romanization and English at the same timestamp
  <song>.txt    plain original / romanization / English, stacked
  <song>.html   a page with all three in columns; plays along with the song
                (click a line to jump there) if the lyrics are synced

Your songs are never changed. A .lrc file you made yourself gets the translation
added to it, with your original kept as <song>.lrc.bak; other existing files are
never replaced unless you pass --force. Everything is worked out before anything
is written, so a failure never leaves half a result, and running it again only
retries what's left.

Usage:
  python3 find_lyrics.py                        # songs from lyrics.folders in config.toml
  python3 find_lyrics.py --list-missing         # list songs without lyrics yet, fetch nothing
  python3 find_lyrics.py --dry-run              # fetch and translate, save nothing
  python3 find_lyrics.py "song.flac" --show     # just this song, and print the result here too
  python3 find_lyrics.py --only-foreign         # skip songs that are already in English
  python3 find_lyrics.py "song.flac" --lyrics "song.txt"    # use the lyrics in this file
  python3 find_lyrics.py --offline              # only use lyrics you already have
  python3 find_lyrics.py --artist X --title Y   # look up a song that isn't a file; prints, doesn't save
  python3 find_lyrics.py --no-translate         # original + romanization only
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from common import (HERE, STOP, atomic_write, bold, dim, find_audio, fit, folder_problem, green, install_stop_handler, load_config,
                    log, normal_ctrl_c, plural, red, require, resolve, section, start_log, text_width, yellow)

require("mutagen")


from fix_album_art import AUDIO_EXTS, clean_text, read_tags
from fix_misidentified_tags import parse_filename
from lyrics_fetch import Lyrics, LyricsError, NotFound, fetch, first_artist, tidy_title
from lyrics_lang import guess_language, has_letters, norm
from lyrics_local import LocalLyricsError, local_lyrics, lyrics_from_file, made_by_us
from lyrics_render import (LAYERS, Line, language_name, layer_label, parse_html_data, parse_layers, render_html, render_lrc,
                           render_terminal, render_txt)
from lyrics_romanize import SUPPORTED, romanize
from lyrics_romanize import status as romanize_status
from lyrics_translate import Stopped, TranslateError, Translator
from lyrics_ui import CatProgress, cat_says, fact, show_banner
from lyrics_ui import width as terminal_width

PROG = os.environ.get("MUSIC_TOOLS_NAME") or "mt"
WEB_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".wav"}
FORMAT_EXTS = {"lrc": ".lrc", "txt": ".txt", "html": ".html"}
CHECKED_FILE = HERE / "lyrics_checked.json"   # what earlier runs found out, so re-runs don't ask again
NOT_FOUND_RECHECK_DAYS = 30                   # lrclib gains lyrics over time
BACKUP_DIR = None    # set from lyrics.backup_dir at startup; None keeps each .bak next to its song
MUSIC_ROOT = None    # music_dir, for mirroring a song's folder under BACKUP_DIR


@dataclass
class Job:
    path: Path
    artist: str = ""
    title: str = ""
    duration: float = None
    lyrics: Lyrics = None
    lang: str = ""
    backend: str = ""          # which translation service was used, "" if none was needed
    saved: dict = field(default_factory=dict)   # format -> "saved" | "exists" | "error: ..."
    status: str = "pending"    # pending | found | done | skipped | failed
    reason: str = ""
    tagfile: object = field(default=None, repr=False, compare=False)  # the song's parsed tags while it's in flight
    certain: bool = False      # the answer is definite (lrclib was asked), so it can be remembered
    romanized: bool = False    # the lyrics are already in Latin letters (romaji); translated from that
    partial: bool = False      # saved without the English yet (translation services were down); finished next run
    was_partial: bool = False  # an earlier run saved it that way, so those files are ours to finish


@dataclass
class Options:
    translate: bool = True
    offline: bool = False
    only_foreign: bool = False
    translate_romanized: bool = True   # lyrics that are already romanized (romaji) get translated too
    lyrics_file: Path = None
    on_start: Callable = None  # called with the song's name when work on it begins


# ---------------------------------------------------------------- reading the library

def load_song_info(job: Job) -> None:
    """Fill in artist/title/length from the song's tags, falling back to the filename."""
    path = job.path
    tags = read_tags(path, keep_file=True)  # one mutagen read covers tags, duration and embedded lyrics
    job.tagfile = tags["file"]
    artist, title = job.artist or tags["artist"].strip(), job.title or tags["title"].strip()
    if not (artist and title):
        parsed = parse_filename(clean_text(path.stem))
        if parsed["pattern"] == "artist_dash_title":
            artist, title = artist or parsed["artist"], title or parsed["title"]
    job.artist, job.title = artist, title or clean_text(path.stem)
    job.duration = job.duration or tags["duration"]


def existing_outputs(path: Path, formats: list) -> dict:
    # not with_suffix twice: "Fred again.. - Title" or "Mr. Brightside" would lose everything after their last dot
    return {fmt: path.with_name(path.stem + FORMAT_EXTS[fmt]) for fmt in formats}


def has_lyrics(path: Path, formats: list, checked: dict = None) -> bool:
    """Whether this song is settled: every file exists, or an earlier run found out there's nothing more to do."""
    entry = (checked or {}).get(str(path)) or {}
    mark = entry.get("status")
    if mark == "english":
        return True
    if mark == "partial":  # saved without English: not done until the translation is in
        return False
    if mark == "own-lrc-kept":  # you said no to changing your .lrc: offer again until it has the translation
        return made_by_us(path.with_suffix(".lrc"))
    if mark == "notfound":
        return time.time() - entry.get("at", 0) < NOT_FOUND_RECHECK_DAYS * 86400
    needed = [f for f in formats if not (f == "lrc" and mark == "unsynced")]  # timing never turned up: no .lrc to wait for
    return all(p.is_file() for p in existing_outputs(path, needed).values())


def load_checked() -> dict:
    try:
        data = json.loads(CHECKED_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_checked(checked: dict) -> None:
    try:
        atomic_write(CHECKED_FILE, json.dumps(checked, ensure_ascii=False, indent=0, sort_keys=True))
    except OSError as e:
        log(f"! couldn't remember what was checked ({CHECKED_FILE.name}): {e.strerror or e}")


# ---------------------------------------------------------------- finding + translating

def process_job(job: Job, translator: Translator, translate: bool, options: Options = None) -> Job:
    """Find lyrics, detect the language, romanize and translate one song. Never raises."""
    options = options or Options(translate=translate)
    if STOP.is_set():
        job.status, job.reason = "failed", "stopped before looking up"
        return job
    try:
        return _process_job(job, translator, translate, options)
    except Stopped:
        job.status, job.reason = "failed", "stopped"
    except Exception as e:  # one odd song must cost one song, never the whole run
        job.status, job.reason = "failed", str(e) or type(e).__name__
    return job


TRAILING_NOISE = re.compile(
    r"[\s\-–|:]*(?:official(?:\s+(?:music\s+)?(?:video|audio|mv|lyric\s+video|channel))?|music\s+video|lyric\s+video|"
    r"lyrics?|mv|audio|hd|4k|試聴動画|公式)\s*$", re.IGNORECASE)


def query_variants(job: Job) -> list:
    """(artist, title) searches to try, best first: the tags, the file's name, both cleaned of YouTube
    decoration, and (when there's no artist) the name of the folder the song is in."""
    out, seen = [], set()

    def add(artist, title):
        title = " ".join((title or "").split())
        key = ((artist or "").strip().casefold(), title.casefold())
        if title and key not in seen:
            seen.add(key)
            out.append(((artist or "").strip(), title))

    def clean(title):
        title = tidy_title(title)
        for _ in range(3):
            title = TRAILING_NOISE.sub("", title).strip()
        return title

    add(job.artist, job.title)
    if job.path.is_file():
        parsed = parse_filename(clean_text(job.path.stem))
        if parsed["pattern"] == "artist_dash_title":
            add(parsed["artist"], parsed["title"])
            # the filename's artist is often righter than the tag (e.g. the tag is a channel/curator
            # name), so it's worth pairing with the cleaned title too, not just the raw tag artist
            add(parsed["artist"], clean(parsed["title"]))
    add(first_artist(job.artist), clean(job.title))
    if not job.artist and job.path.is_file() and job.path.parent.name:
        add(job.path.parent.name, clean(job.title))  # library folders are usually named after the artist
    return out[:6]


def find_original(job: Job, options: Options) -> Lyrics:
    """Your own file, your own .lrc or tags, or lrclib.net: timing beats no timing, yours beats lrclib's."""
    if options.lyrics_file:
        try:
            return lyrics_from_file(options.lyrics_file, job.artist, job.title)
        except (LocalLyricsError, OSError) as e:
            raise LyricsError(str(e)) from e
    local = local_lyrics(job.path, job.artist, job.title, job.tagfile) if job.path.is_file() else None
    if local and local.synced:
        return local
    online, missing = None, ""
    if not options.offline:
        try:
            for artist, title in query_variants(job):
                try:
                    online = fetch(artist, title, duration=job.duration)
                    break
                except NotFound as e:
                    missing = str(e)  # lrclib answered: it doesn't have this spelling; try the next
            job.certain = online is None  # every spelling was answered "not found"
        except LyricsError:
            if not local:
                raise  # can't tell "not found" from "couldn't ask", so say what really happened
    if online and (online.synced or not local):
        return online
    if local:
        return local
    raise NotFound(missing or "no lyrics next to the song or in its tags (and --offline is on)")


def _process_job(job: Job, translator: Translator, translate: bool, options: Options) -> Job:
    if job.path.is_file():  # (a --artist/--title lookup has no file)
        load_song_info(job)
    if options.on_start:
        options.on_start(f"{job.artist} - {job.title}" if job.artist else job.title or job.path.name)
    try:
        lyrics = find_original(job, options)
    finally:
        job.tagfile = None  # parsed tags can hold cover art; don't keep them for every song in the run
    job.lyrics = lyrics
    texts = [l.text for l in lyrics.lines if l.text]
    if not texts:
        raise NotFound("the lyrics are empty")

    # the language, from the writing itself where possible (free, works offline); a service only for the unclear
    written = guess_language("\n".join(texts))
    job.lang = written
    if written == "und":
        job.lang = translator.detect(texts) if translate else ""
        if translate and not job.lang:
            raise TranslateError("couldn't tell what language this is (no translation service answered)")
        if job.lang in SUPPORTED:
            # Latin letters but Japanese/Korean/Chinese/Russian words: the lyrics are already romanized (there's no original
            # writing to keep, so the text as it is stays the "original" and gets an English translation)
            name, readers = language_name(job.lang), getattr(translator, "names", ["claude"])
            if not options.translate_romanized:
                raise NotFound(f"these lyrics are already romanized ({name} in Latin letters) and translating those "
                               f"is switched off (lyrics.translate_romanized)")
            if job.lang != "ja" and "claude" not in readers:
                raise NotFound(f"these lyrics are romanized {name}, which only Claude can translate (Google and DeepL "
                               f"hand it back unchanged). Set ANTHROPIC_API_KEY and add claude to lyrics.translators, "
                               f"or use --lyrics FILE with the original writing")
            job.romanized = True
    if options.only_foreign and job.lang == "en":
        job.status, job.reason, job.certain = "skipped", "already English", True
        return job

    code, job.lang = job.lang, (job.lang + "-Latn" if job.romanized else job.lang)  # "ja-Latn": Japanese in Latin letters
    if code in SUPPORTED and not job.romanized:
        romanize(lyrics.lines, code)

    if translate and code != "en":
        # lines a bilingual .lrc already came with an English version of are kept as they are
        todo = [l.text for l in lyrics.lines if l.text and not l.english and has_letters(l.text)]
        unique = list(dict.fromkeys(todo))  # choruses repeat: translate each line once
        result = None
        if unique:
            try:
                source = f"{code}-romanized" if job.romanized else (written if written in SUPPORTED else "auto")
                result = translator.translate(unique, source)
            except TranslateError as e:
                if not any(l.romaji for l in lyrics.lines):
                    raise TranslateError(f"couldn't translate: {e}") from e  # the original alone isn't worth saving
                # Fallback: no service is answering, but the original and romanization are worth having now.
                # It's marked unfinished, so the next run adds the English (and replaces these files, which are ours).
                job.partial, job.reason = True, f"English still to come: {e}"
        if result:
            by_line = dict(zip(unique, result.lines))
            for line in lyrics.lines:
                if not line.english:
                    english = by_line.get(line.text, "")
                    line.english = english if english and norm(english) != norm(line.text) else ""
            job.backend = result.backend
        elif not job.partial:
            job.backend = "the file"
        if not job.partial and not any(l.english for l in lyrics.lines):
            raise TranslateError("nothing came back translated")

    job.status = "found"
    return job


def find_lyrics(jobs: list, translator: Translator, options: Options, workers: int, quiet: bool) -> None:
    with CatProgress(len(jobs), "Finding lyrics", quiet) as bar:
        options.on_start = bar.start
        with ThreadPoolExecutor(workers) as pool:
            futures = [pool.submit(process_job, j, translator, options.translate, options) for j in jobs]
            for fut in as_completed(futures):
                fut.result()
                bar.advance()


# ---------------------------------------------------------------- saving

def backup_path_for(target: Path) -> Path:
    """Where <name>.bak goes for this file: next to it by default, or mirrored under BACKUP_DIR if set."""
    if not BACKUP_DIR:
        return target.with_name(target.name + ".bak")
    try:
        rel = target.resolve().relative_to(MUSIC_ROOT.resolve())
    except (ValueError, OSError):
        rel = Path(target.name)
    return BACKUP_DIR / rel.with_name(rel.name + ".bak")


def _write(target: Path, text: str, force: bool, replace_ok: bool = False) -> str:
    """Write a file atomically. An existing file is only replaced with --force (or replace_ok), and one that
    music-tools didn't write is copied to <name>.bak first so nothing of yours is ever lost."""
    existed = target.is_file()
    if existed and not (force or replace_ok):
        return "exists"
    tmp = target.with_name(f".{target.name}.tmp")
    note = ""
    try:
        if existed and not made_by_us(target):
            backup = backup_path_for(target)
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            note = f" (yours kept as {backup if BACKUP_DIR else backup.name})"
        tmp.write_bytes(text.encode("utf-8", errors="replace"))
        os.replace(tmp, target)
        return "saved" + note
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return f"error: {getattr(e, 'strerror', None) or e}"


def updates_own_lrc(job: Job, formats: list) -> bool:
    """Whether saving this song would add the translation to a .lrc file you already had."""
    return ("lrc" in formats and job.lyrics.synced and job.lyrics.source == job.path.with_suffix(".lrc").name
            and any(l.romaji or l.english for l in job.lyrics.lines))


def txt_is_ours(path: Path, lines: list) -> bool:
    """A .txt we wrote has exactly the text of one of the layer combinations (it carries no marker of its own)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    combos = [[l for i, l in enumerate(LAYERS) if mask >> i & 1] for mask in range(1, 8)]
    without_english = [Line(l.time, l.text, l.romaji, "") for l in lines]  # what an unfinished run wrote
    return any(text == render_txt(version, c) for version in (lines, without_english) for c in combos)


def save_job(job: Job, formats: list, force: bool, update_own: bool = True, layers=LAYERS, rewrite: bool = False) -> None:
    """Save every format. `rewrite` (used when switching layers) also replaces the files this tool wrote earlier."""
    if STOP.is_set():
        job.saved = {fmt: "error: stopped before saving" for fmt in formats}
        return
    try:
        lines, out = job.lyrics.lines, {}
        own_lrc = job.path.with_suffix(".lrc")
        adds = any(l.romaji or l.english for l in lines)
        if "lrc" in formats:
            if not job.lyrics.synced:
                out["lrc"] = "skipped: lyrics aren't time-synced"
            elif job.lyrics.source == own_lrc.name and not adds:
                out["lrc"] = "exists"  # your .lrc is already English: nothing to add
            else:
                # a .lrc you wrote yourself is the source of these lyrics, so the translation goes into it
                out["lrc"] = _write(own_lrc, render_lrc(lines, job.title, job.artist, layers), force,
                                    replace_ok=(update_own and job.lyrics.source == own_lrc.name)
                                    or (rewrite and made_by_us(own_lrc)))
        if "txt" in formats:
            txt = job.path.with_suffix(".txt")
            out["txt"] = _write(txt, render_txt(lines, layers), force, replace_ok=rewrite and txt_is_ours(txt, lines))
        if "html" in formats:
            audio_href = job.path.name if job.path.suffix.lower() in WEB_AUDIO_EXTS else ""
            page_title = f"{job.artist} - {job.title}" if job.artist else job.title
            html = render_html(lines, page_title, job.artist, job.lang, job.lyrics.source, audio_href, layers)
            out["html"] = _write(job.path.with_suffix(".html"), html, force, replace_ok=rewrite)
        job.saved = out
    except Exception as e:
        job.saved = {fmt: f"error: {e}" for fmt in formats}


# ---------------------------------------------------------------- reporting

def report(todo: list, have: list, args, translator) -> int:
    dry_run = args.dry_run
    done = [j for j in todo if j.status in ("done", "found")]
    skipped = [j for j in todo if j.status == "skipped"]
    failed = [j for j in todo if j.status == "failed"]
    save_errors = [j for j in done if not dry_run and any(r.startswith("error") for r in j.saved.values())]
    width = min(40, max([text_width(j.path.name) for j in done + failed] or [8]))

    if done:
        section("Lyrics found" if dry_run else "Lyrics saved")
        for j in done:
            bits = [language_name(j.lang) if j.lang else "unknown language"]
            if j.lyrics.synced:
                bits.append("synced")
            if j.romanized:
                bits.append("from romanized text")
            if j.partial:
                bits.append("English still to come")
            elif j.backend:
                bits.append(f"translated via {j.backend}")
            if not dry_run:
                bits += [f"{fmt}: {r}" for fmt, r in j.saved.items() if r != "exists"]
            mark = red("✗") if j in save_errors else yellow("~") if j.partial else green("✓")
            print(f"  {mark} {fit(j.path.name, width)}  {dim('  ·  '.join(bits))}")
        if args.show:
            for j in done:
                print(f"\n  {bold(j.path.stem)}\n")
                print("\n".join(render_terminal(j.lyrics.lines, terminal_width(), layers=getattr(args, "layer_list", LAYERS))))
            print()

    if failed:
        section("Couldn't do")
        reasons = Counter(j.reason for j in failed)
        if len(failed) > 6 and len(reasons) <= 2:  # one shared cause: say it once
            for reason, n in reasons.most_common():
                print(f"  {red('✗')} {plural(n, 'song')}: {reason}")
        else:
            for j in failed:
                print(f"  {red('✗')} {fit(j.path.name, width)}  {dim(j.reason or 'no lyrics found')}")
        if any(j.certain for j in failed):
            print(dim(f"\n  Have the lyrics yourself? Save them as a text file and run:"
                      f"\n    {PROG} lyrics \"SONG\" --lyrics FILE"))
        print(dim("  Nothing was saved for these. Run the same command again to retry just them."))
    if getattr(args, "report", None) and failed:
        try:
            text = "".join(f"{j.path}\t{j.reason or 'no lyrics found'}\n" for j in failed)
            Path(args.report).expanduser().write_text(text, encoding="utf-8")
            print(dim(f"\n  Saved the list of {plural(len(failed), 'song')} that weren't found to {args.report}"))
        except OSError as e:
            print(dim(f"\n  ! couldn't write {args.report}: {e.strerror or e}"))
    for name, why in (translator.benched().items() if hasattr(translator, "benched") else ()):
        print(dim(f"\n  ! {name} was set aside: {why}"))

    print("\n  " + dim("─" * 46))
    print("  " + "   ".join(filter(None, [
        green(f"✓ {len(done)} {'found' if dry_run else 'saved'}"),
        dim(f"• {len(have)} already had lyrics"),
        dim(f"• {len(skipped)} already English") if skipped else "",
        (red if failed else dim)(f"✗ {len(failed)} not done"),
    ])))
    print()

    if getattr(args, "left_alone", 0):
        n = args.left_alone
        print(dim(f"  {plural(n, 'of your .lrc file')} {'was' if n == 1 else 'were'} left as {'it is' if n == 1 else 'they are'}. "
                  f"Run with --yes to add the translations {'to it' if n == 1 else 'to them'}."))
        print()
    if STOP.is_set():
        message, mood = "Stopped. Run again to carry on.", "sleep"
    elif failed or save_errors:
        message, mood = f"Mew… {plural(len(failed) + len(save_errors), 'song')} didn't work.", "sad"
    elif done:
        message, mood = f"Purr… {plural(len(done), 'song')} {'found' if dry_run else 'done'}!", "happy"
    else:
        message, mood = "Nothing needed doing. Nap time.", "sleep"
    for line in cat_says(message, mood):
        print(line)
    print()
    return 1 if (failed or save_errors) and not done else 0


# ---------------------------------------------------------------- main

def restore_lrc(files: list, dry_run: bool) -> list:
    """Put each song's <song>.lrc.bak back as <song>.lrc, undoing a translation this tool added, and remove
    the backup. Looks wherever backup_path_for() would have put it (next to the song, or under BACKUP_DIR).
    No lookups, no network."""
    restored = []
    for path in files:
        lrc = path.with_suffix(".lrc")
        backup = backup_path_for(lrc)
        if not backup.is_file():
            continue
        if not dry_run:
            shutil.copy2(backup, lrc)
            backup.unlink()
        restored.append(lrc)
    return restored


def relayer(files: list, formats: list, layers: list, dry_run: bool, quiet: bool, workers: int) -> list:
    """Switch which layers the files show for songs that are already done, from the data stored in each
    page. No lookups and no translating, so it's instant and works offline."""
    jobs = []
    for path in files:
        page = path.with_suffix(".html")
        if not (page.is_file() and made_by_us(page)):
            continue
        try:
            data = parse_html_data(page.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            data = None
        if not data:
            continue
        title = data["title"]
        if data["artist"] and title.startswith(data["artist"] + " - "):
            title = title[len(data["artist"]) + 3:]
        job = Job(path, data["artist"], title or clean_text(path.stem))
        job.lyrics = Lyrics(data["lines"], any(l.time is not None for l in data["lines"]), data["artist"], job.title,
                            data["source"])
        job.lang, job.status = data["lang"], "found"
        jobs.append(job)
    if jobs and not dry_run:
        with CatProgress(len(jobs), "Switching layers", quiet) as bar:
            with ThreadPoolExecutor(min(workers, 4)) as pool:
                for fut in as_completed([pool.submit(save_job, j, formats, False, False, layers, True) for j in jobs]):
                    fut.result()
                    bar.advance()
        for j in jobs:
            j.status = "done"
    return jobs


def confirm_update(own: list) -> bool:
    """Ask before adding translations to .lrc files the user already has (never silently, never in bulk)."""
    print(f"\n  {yellow('?')} {plural(len(own), 'song')} already {'has' if len(own) == 1 else 'have'} a .lrc file of your own.")
    print(dim("    The translation can be added into them (each original is kept next to it as <song>.lrc.bak),"
              "\n    or they can be left exactly as they are, with the new .html pages still written."))
    for j in own[:5]:
        print(dim(f"      {j.path.with_suffix('.lrc').name}"))
    if len(own) > 5:
        print(dim(f"      … and {len(own) - 5} more"))
    if not sys.stdin.isatty():
        print(dim("    Not asking (no terminal): leaving them alone. Use --yes to add the translations."))
        return False
    try:
        with normal_ctrl_c():
            return input(f"\n  Add the translation to {plural(len(own), 'file')}? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/folders to find lyrics for (default: lyrics.folders from the config)")
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--artist", help="look up one song by artist/title instead of scanning files; prints, doesn't save")
    ap.add_argument("--title", help="used with --artist")
    ap.add_argument("--formats", help="comma-separated: lrc,txt,html (default: lyrics.formats from the config)")
    ap.add_argument("--list-missing", action="store_true", help="only list songs without lyrics yet; no fetching")
    ap.add_argument("--dry-run", action="store_true", help="fetch and translate but don't save anything")
    ap.add_argument("--show", action="store_true", help="also print the lyrics side by side here")
    ap.add_argument("--force", action="store_true", default=None, help="replace lyrics files that already exist (yours are kept as .bak)")
    ap.add_argument("--only-foreign", action="store_true", default=None, help="skip songs whose lyrics are already English (the default)")
    ap.add_argument("--include-english", action="store_true", help="also save lyrics for songs that are already English")
    ap.add_argument("--no-romanized", action="store_true", help="don't translate lyrics that are already romanized (romaji); "
                    "by default they're translated too (Korean/Chinese romanization needs Claude)")
    ap.add_argument("--layers", metavar="LIST", help="what each line shows: any of original (kanji/hangul), romanization (romaji), "
                    "english, e.g. original,english (default: lyrics.layers, all three)")
    ap.add_argument("--relayer", action="store_true", help="switch the layers of songs that are already done (no lookups, works offline)")
    ap.add_argument("--restore-lrc", action="store_true", help="put back the .lrc.bak this tool made before adding a "
                    "translation to your .lrc, undoing that (no lookups, works offline; combine with --dry-run to preview)")
    ap.add_argument("--limit", type=int, metavar="N", help="only do the first N songs that need it (good for a trial run)")
    ap.add_argument("--report", metavar="FILE", help="save the not-found/failed list (path + reason, one per line) as a plain text file")
    ap.add_argument("--yes", action="store_true", help="don't ask before adding translations to your existing .lrc files")
    ap.add_argument("--lyrics", metavar="FILE", help="use the lyrics in this .lrc/.txt file for the one song given")
    ap.add_argument("--offline", action="store_true", help="don't look anything up online; use .lrc files and tags only")
    ap.add_argument("--no-translate", action="store_true", help="skip the English translation")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel lookups")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()

    if args.title and not args.artist:
        sys.exit("--title needs --artist.")

    cfg = load_config(args.config)
    start_log("lyrics", cfg)
    opts = dict(cfg["lyrics"])
    global BACKUP_DIR, MUSIC_ROOT
    MUSIC_ROOT = Path(cfg["music_dir"])
    BACKUP_DIR = resolve(opts["backup_dir"], cfg["music_dir"]) if opts["backup_dir"] else None
    if args.force:
        opts["force"] = True
    if args.only_foreign:
        opts["only_foreign"] = True
    if args.include_english:
        opts["only_foreign"] = False
    if args.formats:
        opts["formats"] = [f.strip() for f in args.formats.split(",") if f.strip()]
        if not opts["formats"] or not set(opts["formats"]) <= set(FORMAT_EXTS):
            sys.exit(f"--formats should be a list made of: {', '.join(FORMAT_EXTS)}")
    try:
        layers = parse_layers(args.layers or opts["layers"])
    except ValueError as e:
        sys.exit(f"{'--layers' if args.layers else 'lyrics.layers'}: {e}")
    args.layer_list = layers
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    translator = Translator(opts["translators"], opts["claude_model"])
    options = Options(translate=not args.no_translate, offline=args.offline, only_foreign=opts["only_foreign"],
                      translate_romanized=opts["translate_romanized"] and not args.no_romanized)
    show_banner()

    # --artist/--title: look up one song that isn't necessarily in your library, print it, done.
    if args.artist:
        job = Job(Path("-"), args.artist, args.title or "")
        process_job(job, translator, options.translate, options)
        if job.status == "failed":
            sys.exit(f"Could not find lyrics: {job.reason}")
        print(f"  {bold(job.lyrics.artist + ' - ' + job.lyrics.title if job.lyrics.artist else job.lyrics.title)}"
              f"  {dim(language_name(job.lang) if job.lang else '')}\n")
        for line in render_terminal(job.lyrics.lines, terminal_width(), layers=layers):
            print(line)
        print()
        return

    if args.lyrics:
        options.lyrics_file = Path(args.lyrics).expanduser()
        if len(args.paths) != 1 or not Path(args.paths[0]).expanduser().is_file():
            sys.exit("--lyrics needs exactly one song file, e.g.  --lyrics lyrics.txt \"song.flac\"")
        if not options.lyrics_file.is_file():
            sys.exit(f"Lyrics file not found: {options.lyrics_file}")
        opts["force"] = True  # you asked for these lyrics for this song

    paths = args.paths or [resolve(f, cfg["music_dir"]) for f in opts["folders"]]
    for p in paths:
        problem = folder_problem(p) if not Path(p).expanduser().is_file() else ""
        if problem:
            sys.exit(f"Can't use {p}:\n  {problem}")
    files = list(find_audio(paths, AUDIO_EXTS))
    fact("Songs", f"{len(files)} in {plural(len(paths), 'folder')}")
    if not files:
        print(f"\n  {yellow('•')} No songs found there.\n")
        return
    if args.restore_lrc:
        restored = restore_lrc(files, args.dry_run)
        if not restored:
            print(f"\n  {yellow('•')} No .lrc.bak backups to restore there.\n")
            return
        section("Would restore" if args.dry_run else "Restored")
        for lrc in restored:
            print(f"  {green('✓')} {lrc.name}")
        count = plural(len(restored), "file")
        if args.dry_run:
            print(f"\n  {dim(f'{count} would be restored. Run without --dry-run to do it.')}\n")
        else:
            print(f"\n  {green(f'✓ {count} restored')}\n")
        return
    if args.relayer:
        fact("Layers", layer_label(layers))
        switched = relayer(files, opts["formats"], layers, args.dry_run, quiet, workers)
        if not switched:
            print(f"\n  {yellow('•')} No finished songs to switch: this only works on songs whose .html this tool wrote.\n")
            return
        return report(switched, [], args, None)
    jobs = [Job(p) for p in files]

    fresh_look = opts["force"] or args.include_english  # asking for something new overrides what was remembered
    checked = {} if fresh_look else load_checked()
    for j in jobs:
        j.was_partial = (checked.get(str(j.path)) or {}).get("status") == "partial"
    have, todo = [], []
    for j in jobs:  # one pass; `j not in have` would compare whole dataclasses field by field, O(n^2)
        (have if not opts["force"] and has_lyrics(j.path, opts["formats"], checked) else todo).append(j)
    if args.limit is not None:
        todo = todo[:max(0, args.limit)]
    fact("Lyrics", f"{len(jobs)}  {dim('·')}  {len(have)} already done  {dim('·')}  {bold(str(len(todo)))} to find")
    if args.list_missing:
        width = min(40, max([text_width(j.path.name) for j in todo] or [8]))
        section("Without lyrics" if todo else "Every song has lyrics")
        for j in todo:
            print(f"  {yellow('•')} {fit(j.path.name, width)}")
        print()
        return
    if not todo:
        print(f"\n  {green('✓')} Every song already has lyrics.\n")
        for line in cat_says("Nothing needed doing. Nap time.", "sleep"):
            print(line)
        print()
        return

    fact("Layers", layer_label(layers))
    if options.translate and hasattr(translator, "names"):
        fact("Translate", " → ".join(translator.names) or red("no service available"))
        for name, why in translator.skipped.items():
            print(dim(f"  {'':11} {name} skipped: {why}"))
        if not translator.names:
            sys.exit("No translation service can be used. Add google or mymemory to lyrics.translators, "
                     "or set the key for the others (see config.example.toml). Use --no-translate to skip translating.")
    engines = romanize_status()
    fact("Romanize", "  ".join(f"{lang}: {engine or yellow('not installed')}" for lang, engine in engines.items()))
    if not all(engines.values()):
        print(dim(f"  {'':11} Missing romanization leaves that column out. Run ./setup.sh to install it."))
    print()

    find_lyrics(todo, translator, options, workers, quiet)

    ready = [j for j in todo if j.status == "found"]
    update_own = True
    own = [j for j in ready if updates_own_lrc(j, opts["formats"])]
    if own and not args.dry_run and not opts["force"] and not args.yes:
        update_own = confirm_update(own)
        args.left_alone = 0 if update_own else len(own)
    if ready and not args.dry_run:
        with CatProgress(len(ready), "Saving lyrics", quiet) as bar:
            with ThreadPoolExecutor(min(workers, 4)) as pool:  # disk-bound; don't thrash the drive
                for fut in as_completed([pool.submit(save_job, j, opts["formats"], opts["force"], update_own, layers,
                                                     j.was_partial) for j in ready]):
                    fut.result()
                    bar.advance()
        for j in ready:
            j.status = "done"

    if not args.dry_run and not options.lyrics_file:
        remembered = load_checked()
        for j in todo:
            key, now = str(j.path), time.time()
            if j.status == "failed" and j.certain and not args.offline:
                remembered[key] = {"status": "notfound", "at": now}
            elif j.status == "skipped":
                remembered[key] = {"status": "english", "at": now}
            elif j.status == "done" and j.partial:
                remembered[key] = {"status": "partial", "at": now}
            elif j.status == "done" and not update_own and updates_own_lrc(j, opts["formats"]):
                remembered[key] = {"status": "own-lrc-kept", "at": now}
            elif j.status == "done" and not j.lyrics.synced and "lrc" in opts["formats"]:
                remembered[key] = {"status": "unsynced", "at": now}
            elif j.status == "done":
                remembered.pop(key, None)
        save_checked(remembered)

    return report(todo, have, args, translator)


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
