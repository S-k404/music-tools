#!/usr/bin/env python3
"""
fix_misidentified_tags.py

Fix audio files that MusicBrainz Picard (or another auto-tagger) matched
against the wrong studio album.

Live sets, DJ mixes and unreleased tracks usually have descriptive filenames:
  "Fred again.. & Thomas Bangalter (USB002, Alexandra Palace, London 27 February 2026).flac"
but Picard may tag them as an unrelated album track (Title "Delilah (pull me
out of this)", Album "Actual Life 3", ...). The filename is right, the tags
are wrong. This script rebuilds the tags from the filename.

What it does:
  - Scans a folder recursively (.flac, .mp3, .m4a, .wav, .aiff, .ogg, .opus)
  - Parses "Artist - Title", "Artist (Details)", "01 - Title" and plain names
  - Sets Title / Artist / Album from the filename
  - Removes MusicBrainz / AcoustID tags that link the file to the wrong release
  - Dry run by default: prints current vs. proposed tags, writes nothing
    until you pass --apply
  - Reads files in parallel with a progress bar; settings come from config.toml
"""

import argparse
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import DEFAULTS, STOP, find_audio, start_log, install_stop_handler, load_config, log, progress, require

require("mutagen")

import mutagen
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK
from mutagen.mp4 import MP4

# ==============================================================================
# CONSTANTS
# ==============================================================================
# User settings (music_dir, title/album mode, ...) live in config.toml, see
# config.example.toml. Defaults used by determine_proposed_tags():
TITLE_MODE = DEFAULTS["tags"]["title_mode"]
UPDATE_ARTIST = DEFAULTS["tags"]["update_artist"]
RESET_ALBUM = DEFAULTS["tags"]["album_mode"]

# Supported audio extensions
SUPPORTED_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".mp4",
    ".wav",
    ".wave",
    ".aif",
    ".aiff",
    ".ogg",
    ".opus",
}

# Picard / MusicBrainz tags to purge
MUSICBRAINZ_VORBIS_PREFIXES = (
    "musicbrainz_",
    "acoustid_",
    "musicip_",
)

MUSICBRAINZ_VORBIS_EXACT = {
    "asin",
    "barcode",
    "isrc",
    "catalognumber",
    "label",
    "media",
    "releasestatus",
    "releasetype",
    "releasecountry",
    "script",
    "artistsort",
    "albumartistsort",
}

# Song title annotation keywords (remix, feat, edit)
REMIX_FEAT_WORDS = {
    "remix",
    "re-mix",
    "edit",
    "vip",
    "dub",
    "acoustic",
    "instrumental",
    "version",
    "flip",
}


# ==============================================================================
# FILENAME PARSING
# ==============================================================================
def is_song_annotation(text: str) -> bool:
    """Returns True if text in parentheses represents a remix/feat annotation rather than venue/event."""
    text_low = text.lower().strip()
    if text_low.startswith(("feat.", "feat ", "ft.", "ft ")):
        return True
    return any(w in text_low for w in REMIX_FEAT_WORDS)


def is_cjk(s: str) -> bool:
    """Returns True if string contains CJK characters (Japanese, Chinese, Korean)."""
    return any(("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af") for ch in s)


GENERIC_STOPWORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "feat", "ft", "remix", "re-mix", "mix", "edit", "vip", "dub", "version", "original", "flac", "mp3"
}

LIVE_KEYWORDS = {
    "boiler room", "live @", "live at", "dj set", "essential mix", "usb00", "b2b", "mixmag", "cercle"
}


def extract_core_words(s: str) -> set:
    if not s:
        return set()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"^(\d{1,3})[\s\.\-_]+", "", s)
    tokens = re.findall(r"[a-zA-Z0-9]+", s.lower())
    return {w for w in tokens if w not in GENERIC_STOPWORDS}


def normalize_simple(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"^(\d{1,3})[\s\.\-_]+", "", s)
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def is_severe_mismatch(stem: str, embedded_title: Optional[str]) -> bool:
    """
    Carefully determines if an embedded title is a genuine severe mismatch from the filename.
    Protects against false positives (transliterations, punctuation differences, typo variations).
    """
    if not embedded_title or not stem:
        return False

    # Protect CJK/Japanese/Korean releases (e.g. Romaji vs Kanji title differences)
    if is_cjk(stem) or is_cjk(embedded_title):
        return False

    stem_low = stem.lower()
    title_low = embedded_title.lower()

    # If filename is a live set/mix and Picard tagged it as a non-live studio track
    if any(k in stem_low for k in LIVE_KEYWORDS):
        if not any(k in title_low for k in LIVE_KEYWORDS):
            return True

    norm_stem = normalize_simple(stem)
    norm_title = normalize_simple(embedded_title)
    if not norm_stem or not norm_title:
        return False

    # Substring check (e.g. track numbers, subtitle additions)
    if norm_title in norm_stem or norm_stem in norm_title:
        return False

    # Overall string similarity (e.g. Novacane vs novocaine)
    if SequenceMatcher(None, norm_stem, norm_title).ratio() > 0.60:
        return False

    core_stem = extract_core_words(stem)
    core_title = extract_core_words(embedded_title)
    if not core_stem or not core_title:
        return False

    # Token-level similarity check (checks if any core word is a typo/variation of another)
    for ws in core_stem:
        for wt in core_title:
            if ws == wt or SequenceMatcher(None, ws, wt).ratio() >= 0.75:
                return False

    return True


def parse_filename(stem: str) -> Dict[str, Any]:
    """
    Intelligently parses an audio filename stem into track number, artist, title,
    and annotations.

    Supported patterns:
      1. Leading track numbers: "01 - Title", "01. Title", "1 Title"
      2. "Artist - Title" (handles hyphens -, –, —, and pipes |, ｜)
      3. "Artist (Details)" or "Artist [Details]" (for live sets / venue recordings)
      4. "Title (Remix / Feat)"
      5. Fallback clean filename
    """
    clean = stem.strip()

    # 1. Strip leading track number if present e.g. "01 - ", "01. ", "1. ", "01 "
    track_num = None
    m_track = re.match(r"^(\d{1,3})(?:\s*[-–—\.]\s*|\s+)(.*)$", clean)
    working_name = clean
    if m_track:
        track_num = m_track.group(1)
        working_name = m_track.group(2).strip()

    # 2. Check for "Artist - Title" or "Artist | Title"
    dash_match = re.search(r"^(.*?)\s+(?:[-–—]{1,2}|[|｜])\s+(.*)$", working_name)
    if dash_match:
        artist = dash_match.group(1).strip()
        title = dash_match.group(2).strip()
        if artist and title:
            return {
                "pattern": "artist_dash_title",
                "stem": clean,
                "clean_stem": working_name,
                "track": track_num,
                "artist": artist,
                "title": title,
                "details": None,
            }

    # 3. Check for "Artist (Details)" or "Artist [Details]" at the end
    paren_match = re.search(r"^(.*?)\s+([\({\[])(.*)([\)}\]])$", working_name)
    if paren_match:
        candidate_artist = paren_match.group(1).strip()
        candidate_details = paren_match.group(3).strip()
        if candidate_artist and candidate_details:
            if is_song_annotation(candidate_details):
                # e.g. "solo (KETTAMA remix)" - this is a song title, not artist + details
                return {
                    "pattern": "title_with_annotation",
                    "stem": clean,
                    "clean_stem": working_name,
                    "track": track_num,
                    "artist": None,
                    "title": working_name,
                    "details": candidate_details,
                }
            else:
                # e.g. "Fred again.. & Thomas Bangalter (USB002, Alexandra Palace, London 27 February 2026)"
                return {
                    "pattern": "artist_paren_details",
                    "stem": clean,
                    "clean_stem": working_name,
                    "track": track_num,
                    "artist": candidate_artist,
                    "title": candidate_details,
                    "details": candidate_details,
                }

    # 4. Fallback: single title directly from clean filename
    return {
        "pattern": "raw",
        "stem": clean,
        "clean_stem": working_name,
        "track": track_num,
        "artist": None,
        "title": working_name if working_name else clean,
        "details": None,
    }


def determine_proposed_tags(
    parsed: Dict[str, Any],
    parent_folder: str,
    title_mode: str = TITLE_MODE,
    update_artist: bool = UPDATE_ARTIST,
    reset_album: str = RESET_ALBUM,
) -> Dict[str, Optional[str]]:
    """
    Computes proposed Title, Artist, and Album tags from the parsed filename
    and chosen configuration.
    """
    pattern = parsed["pattern"]
    clean_stem = parsed["stem"]

    # Determine Title
    if title_mode == "stem":
        proposed_title = clean_stem
    elif title_mode == "parsed":
        proposed_title = parsed["title"]
    elif title_mode == "auto":
        if pattern == "artist_dash_title":
            proposed_title = parsed["title"]
        elif pattern == "title_with_annotation" or (pattern == "raw" and parsed["track"]):
            # "03 - Kyle" -> "Kyle" (the number goes in the track tag)
            proposed_title = parsed["clean_stem"]
        else:
            # For live sets, full stem keeps full context (artists, venue, date)
            proposed_title = clean_stem
    else:
        proposed_title = clean_stem

    # Determine Artist
    proposed_artist = parsed["artist"] if update_artist else None

    # Determine Album
    proposed_album = None
    if reset_album == "folder":
        proposed_album = parent_folder
    elif reset_album == "title":
        proposed_album = proposed_title
    elif reset_album == "clear":
        proposed_album = ""  # signal to delete album tag
    # if "keep", proposed_album remains None

    return {
        "title": proposed_title,
        "artist": proposed_artist,
        "album": proposed_album,
        "track": str(int(parsed["track"])) if parsed.get("track") else None,
    }


# ==============================================================================
# MUTAGEN TAG EXTRACTION & MANIPULATION
# ==============================================================================
PICARD_MARKERS = ("musicbrainz", "acoustid", "musicip")
PICARD_TXXX_MARKERS = PICARD_MARKERS + ("asin", "barcode")
PICARD_ID3_FRAMES = ("TSOP", "TSO2", "TSRC")  # sort orders & ISRC written by Picard
TRACK_KEYS_VORBIS = ("tracknumber", "totaltracks", "tracktotal", "discnumber", "totaldiscs", "disctotal")


def tag_kind(audio: Any) -> Optional[str]:
    """Classify a mutagen file by tag system: 'vorbis', 'id3', 'mp4' or None."""
    if isinstance(audio, MP4):
        return "mp4"
    if isinstance(audio.tags, ID3) or type(audio).__name__ in ("MP3", "WAVE", "AIFF"):
        return "id3"
    if audio.tags is None or hasattr(audio.tags, "keys"):
        # FLAC, Ogg Vorbis, Opus (VComment tags)
        return "vorbis" if type(audio).__name__ in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC") else None
    return None


def is_picard_key(kind: str, key: str, frame: Any = None) -> bool:
    k_low = key.lower()
    if kind == "vorbis":
        return k_low.startswith(MUSICBRAINZ_VORBIS_PREFIXES) or k_low in MUSICBRAINZ_VORBIS_EXACT
    if kind == "id3":
        if key.startswith("UFID"):
            return "musicbrainz" in getattr(frame, "owner", "").lower()
        if key.startswith("TXXX"):
            desc = getattr(frame, "desc", "").lower()
            return any(x in desc for x in PICARD_TXXX_MARKERS)
        return key in PICARD_ID3_FRAMES or any(x in k_low for x in PICARD_MARKERS)
    if kind == "mp4":
        return any(x in k_low for x in PICARD_MARKERS)
    return False


def _first(tags: Any, key: str) -> Optional[str]:
    if not tags:
        return None
    val = tags.get(key)
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if isinstance(val, tuple):  # mp4 trkn = (track, total)
        val = val[0]
    return str(val) if val is not None else None


def get_current_tags(audio: Any) -> Dict[str, Any]:
    """Read title, artist, album, track number and Picard tag keys from any supported file."""
    result: Dict[str, Any] = {
        "title": None,
        "artist": None,
        "album": None,
        "tracknumber": None,
        "picard_keys": [],
    }
    kind = tag_kind(audio) if audio is not None else None
    tags = audio.tags if audio is not None else None
    if not kind or not tags:
        return result

    if kind == "vorbis":
        keys = {"title": "title", "artist": "artist", "album": "album", "tracknumber": "tracknumber"}
        for field, key in keys.items():
            result[field] = _first(tags, key)
        items = [(k, None) for k in tags.keys()]
    elif kind == "id3":
        for field, key in (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"), ("tracknumber", "TRCK")):
            if key in tags:
                result[field] = str(tags[key])
        items = list(tags.items())
    else:  # mp4
        for field, key in (("title", "\xa9nam"), ("artist", "\xa9ART"), ("album", "\xa9alb"), ("tracknumber", "trkn")):
            result[field] = _first(tags, key)
        items = [(k, None) for k in tags.keys()]

    result["picard_keys"] = [k for k, frame in items if is_picard_key(kind, k, frame)]
    return result


def apply_tags_to_file(
    audio: Any,
    proposed_title: str,
    proposed_artist: Optional[str] = None,
    proposed_album: Optional[str] = None,
    proposed_track: Optional[str] = None,
    clear_picard: bool = True,
    clear_tracks: bool = True,
) -> Tuple[bool, List[str]]:
    """
    Write the proposed tags and strip Picard tags on the audio object.
    If the filename carried a track number ("01 - Title") it is written instead
    of being cleared. Returns (saved, actions_taken).
    """
    kind = tag_kind(audio)
    if kind is None:
        return False, ["Unsupported audio container format"]
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    actions: List[str] = [f"Set title='{proposed_title}'"]

    if kind == "vorbis":
        tags["title"] = [proposed_title]
        if proposed_artist:
            tags["artist"] = [proposed_artist]
            for k in ("albumartist", "artists"):
                if k in tags:
                    tags[k] = [proposed_artist]
        if proposed_album == "":
            for k in ("album", "albumartist"):
                if k in tags:
                    del tags[k]
        elif proposed_album is not None:
            tags["album"] = [proposed_album]
        if clear_tracks:
            for k in TRACK_KEYS_VORBIS:
                if k in tags:
                    del tags[k]
        if proposed_track:
            tags["tracknumber"] = [proposed_track]
        picard = [k for k in list(tags.keys()) if is_picard_key(kind, k)]

    elif kind == "id3":
        tags.setall("TIT2", [TIT2(encoding=3, text=[proposed_title])])
        if proposed_artist:
            tags.setall("TPE1", [TPE1(encoding=3, text=[proposed_artist])])
            if "TPE2" in tags:
                tags.setall("TPE2", [TPE2(encoding=3, text=[proposed_artist])])
        if proposed_album == "":
            tags.delall("TALB")
            tags.delall("TPE2")
        elif proposed_album is not None:
            tags.setall("TALB", [TALB(encoding=3, text=[proposed_album])])
        if clear_tracks:
            tags.delall("TRCK")
            tags.delall("TPOS")
        if proposed_track:
            tags.setall("TRCK", [TRCK(encoding=3, text=[proposed_track])])
        picard = [k for k, frame in list(tags.items()) if is_picard_key(kind, k, frame)]

    else:  # mp4
        tags["\xa9nam"] = [proposed_title]
        if proposed_artist:
            tags["\xa9ART"] = [proposed_artist]
            if "aART" in tags:
                tags["aART"] = [proposed_artist]
        if proposed_album == "":
            for k in ("\xa9alb", "aART"):
                if k in tags:
                    del tags[k]
        elif proposed_album is not None:
            tags["\xa9alb"] = [proposed_album]
        if clear_tracks:
            for k in ("trkn", "disk"):
                if k in tags:
                    del tags[k]
        if proposed_track:
            tags["trkn"] = [(int(proposed_track), 0)]
        picard = [k for k in list(tags.keys()) if is_picard_key(kind, k)]

    if proposed_artist:
        actions.append(f"Set artist='{proposed_artist}'")
    if proposed_album == "":
        actions.append("Cleared album tag")
    elif proposed_album is not None:
        actions.append(f"Set album='{proposed_album}'")
    if clear_tracks:
        actions.append("Cleared track/disc numbers")
    if proposed_track:
        actions.append(f"Set track={proposed_track}")
    if clear_picard and picard:
        for k in picard:
            del tags[k]
        actions.append(f"Removed {len(picard)} Picard/MB tag(s)")

    audio.save()
    return True, actions


# ==============================================================================
# MAIN PROCESSING & SCANNER
# ==============================================================================
def process_audio_file(filepath: Path, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Wrapper that never raises, so one bad file can't stop the whole run."""
    if STOP.is_set():
        return None
    try:
        return _process_audio_file(filepath, opts)
    except Exception as err:
        return {"lines": [f"[!] {filepath.name}: unexpected error: {err}"], "error": True, "path": filepath}


def _process_audio_file(filepath: Path, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Inspect one file: read current tags, compute proposed tags from the
    filename and apply them unless dry_run. Returns the report lines instead of
    printing, so files can be processed in parallel and still print in order.
    """
    # macOS stores filenames decomposed (NFD); compose so CJK checks and tags are right
    stem = unicodedata.normalize("NFC", filepath.stem)
    parent_folder = unicodedata.normalize("NFC", filepath.parent.name)

    try:
        audio = mutagen.File(str(filepath))
    except Exception as err:
        return {"lines": [f"[!] Could not read {filepath.name}: {err}"], "error": True, "path": filepath}
    if audio is None:
        return None

    current = get_current_tags(audio)
    if opts["only_severe"] and not is_severe_mismatch(stem, current["title"]):
        return None  # most files: skip the rest of the work
    parsed = parse_filename(stem)
    proposed = determine_proposed_tags(
        parsed=parsed,
        parent_folder=parent_folder,
        title_mode=opts["title_mode"],
        update_artist=opts["update_artist"],
        reset_album=opts["album_mode"],
    )

    if opts.get("set_artist"):
        proposed["artist"] = opts["set_artist"]

    current_title = current["title"] or "<None>"
    proposed_title = proposed["title"] or stem
    title_differs = current_title.strip().lower() != proposed_title.strip().lower()
    has_picard = len(current["picard_keys"]) > 0

    if opts["only_severe"] and not is_severe_mismatch(stem, current["title"]):
        return None
    if opts["only_mismatched"] and not title_differs and not has_picard:
        return None

    lines = [
        "=" * 80,
        f"File: {filepath}",
        f"  Filename Stem:          {stem}",
        f"  Detected Pattern:       {parsed['pattern']}",
        f"  Current Embedded Title: {current_title}",
        f"  Proposed New Title:     {proposed_title}",
    ]
    if proposed["artist"]:
        lines.append(f"  Current Artist:         {current['artist'] or '<None>'}")
        lines.append(f"  Proposed New Artist:    {proposed['artist']}")
    if proposed["album"] is not None:
        lines.append(f"  Current Album:          {current['album'] or '<None>'}")
        lines.append(f"  Proposed New Album:     {proposed['album'] or '<Clear Tag>'}")
    if proposed["track"]:
        lines.append(f"  Track Number:           {current['tracknumber'] or '<None>'} -> {proposed['track']}")
    if current["picard_keys"]:
        keys = current["picard_keys"]
        lines.append(f"  Picard Tags to Clear:   {len(keys)} tag(s) -> {', '.join(keys[:6])}{'...' if len(keys) > 6 else ''}")

    error = False
    if opts["dry_run"]:
        lines.append("  -> [DRY RUN] No changes written to disk.")
    else:
        try:
            success, actions = apply_tags_to_file(
                audio=audio,
                proposed_title=proposed_title,
                proposed_artist=proposed["artist"],
                proposed_album=proposed["album"],
                proposed_track=proposed["track"],
                clear_picard=opts["clear_picard"],
                clear_tracks=opts["clear_tracks"],
            )
            lines.append(f"  -> [{'APPLIED' if success else 'FAILED'}] {'; '.join(actions)}")
            error = not success
        except Exception as write_err:
            lines.append(f"  -> [ERROR] Failed to save tags: {write_err}")
            error = True

    return {
        "lines": lines,
        "filepath": filepath,
        "title_differs": title_differs,
        "has_picard": has_picard,
        "error": error,
    }


def scan(paths: List[Path], opts: Dict[str, Any]) -> None:
    """Process every audio file under paths in parallel, printing results in order."""
    audio_files = list(find_audio(paths, SUPPORTED_EXTENSIONS, opts["filter"]))
    total_files = len(audio_files)
    log(f"Found {total_files} audio file(s) to process.")
    log(f"Mode: {'DRY RUN (preview only)' if opts['dry_run'] else 'APPLY (writing to disk)'}")
    log(f"Title Mode: '{opts['title_mode']}' | Album Mode: '{opts['album_mode']}' | Clear Picard Tags: {opts['clear_picard']}")
    if opts["only_severe"]:
        log("Filter: ONLY SEVERE MISMATCHES (protecting Japanese transliterations, typos, and normal tracks)")
    log()

    results = []
    with ThreadPoolExecutor(opts["workers"]) as pool:
        work = pool.map(lambda p: process_audio_file(p, opts), audio_files)
        for res in progress(work, total_files, "Checking tags", opts["no_progress"]):
            if res is not None:
                for line in res["lines"]:
                    log(line)
                results.append(res)

    shown = [r for r in results if "filepath" in r]
    errors = [r for r in results if r.get("error")]
    print("\n" + "=" * 80)
    print("SUMMARY REPORT")
    print("=" * 80)
    print(f"Total audio files scanned:   {total_files}")
    print(f"Files inspected / displayed: {len(shown)}")
    print(f"Files with title mismatches: {sum(r['title_differs'] for r in shown)}")
    print(f"Files with Picard/MB tags:   {sum(r['has_picard'] for r in shown)}")
    if errors:
        print(f"\nFailed ({len(errors)}):")
        for r in errors:
            print(f"  {r.get('filepath') or r.get('path')}")
            print(f"    {r['lines'][-1].strip()}")
    if STOP.is_set():
        print("\nStopped early (Ctrl-C). Files already written were finished; the rest were not touched.")
    if opts["dry_run"]:
        print("\nDry run: no files were modified. Re-run with --apply to write changes.")
    else:
        print(f"\nUpdated {len(shown) - len(errors)} file(s).")


# ==============================================================================
# CLI ENTRY POINT
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Fix audio files that MusicBrainz Picard tagged with the wrong metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Settings default to the [tags] section of config.toml; flags override them.

Examples:
  # Preview changes for music_dir from the config (DRY RUN):
  python3 fix_misidentified_tags.py

  # Preview ONLY severe mismatches across the entire library:
  python3 fix_misidentified_tags.py --only-severe

  # Apply fixes ONLY to severe mismatches:
  python3 fix_misidentified_tags.py --only-severe --apply

  # Target a specific folder:
  python3 fix_misidentified_tags.py "/path/to/Music/Fred again/USB002" --apply
        """,
    )
    parser.add_argument("paths", nargs="*", help="files/folders to scan (default: music_dir from the config)")
    parser.add_argument("--config", help="path to a config.toml")
    parser.add_argument("--apply", action="store_true", help="write changes to disk (default is a dry-run preview)")
    parser.add_argument("--title-mode", choices=["stem", "parsed", "auto"],
                        help="'auto' (clean titles, full context for live sets), 'stem' (whole filename) or 'parsed'")
    parser.add_argument("--album-mode", choices=["folder", "clear", "title", "keep"],
                        help="'folder' (parent folder name), 'clear', 'title' or 'keep'")
    parser.add_argument("--no-artist", action="store_true", help="don't update the Artist tag")
    parser.add_argument("--set-artist", metavar="NAME",
                        help="write NAME as the Artist tag (for songs the filename can't name); needs a path or --filter")
    parser.add_argument("--keep-picard-tags", action="store_true", help="don't remove Picard/MusicBrainz/AcoustID tags")
    parser.add_argument("--keep-tracks", action="store_true", help="don't clear track and disc numbers")
    parser.add_argument("--filter", help="only process files whose path contains this text (case-insensitive)")
    parser.add_argument("--only-mismatched", action="store_true",
                        help="only show files whose title differs or that have Picard tags")
    parser.add_argument("--only-severe", action="store_true",
                        help="careful mode: only true mismatches (skips CJK transliterations, typos, normal albums)")
    parser.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64",
                        help="files to process in parallel")
    parser.add_argument("--no-progress", action="store_true", help="hide the progress bar")
    args = parser.parse_args()

    cfg = load_config(args.config)
    start_log("tags", cfg)
    t = cfg["tags"]
    opts = {
        "dry_run": not args.apply,
        "title_mode": args.title_mode or t["title_mode"],
        "album_mode": args.album_mode or t["album_mode"],
        "update_artist": t["update_artist"] and not args.no_artist,
        "set_artist": (args.set_artist or "").strip(),
        "clear_picard": t["clear_picard"] and not args.keep_picard_tags,
        "clear_tracks": t["clear_tracks"] and not args.keep_tracks,
        "filter": args.filter,
        "only_mismatched": args.only_mismatched,
        "only_severe": args.only_severe,
        "workers": args.workers or cfg["workers"],
        "no_progress": args.no_progress,
    }
    if cfg.get("_path"):
        log(f"Using config {cfg['_path']}")

    paths = [Path(p) for p in args.paths] or [Path(cfg["music_dir"])]
    if args.set_artist is not None and not opts["set_artist"]:
        sys.exit("--set-artist needs a name.")
    if opts["set_artist"] and not args.paths and not args.filter:
        sys.exit("--set-artist would rename the artist on every song: name the file(s) or add --filter TEXT.")

    # Safety guard: applying to the whole library without a filter needs confirmation
    whole_library = not args.paths or any(p.resolve() == Path(cfg["music_dir"]).resolve() for p in paths)
    if opts["dry_run"] is False and whole_library and not args.filter and not args.only_severe:
        print("\n[!] CAUTION: You are about to modify tags across your ENTIRE library:")
        print(f"    Path: {cfg['music_dir']}")
        print("    This will rewrite tags and remove MusicBrainz IDs from ALL matched files.")
        if input("    Type 'YES' to continue: ").strip() != "YES":
            print("Aborted. No files were modified.")
            sys.exit(0)

    scan(paths, opts)


if __name__ == "__main__":
    install_stop_handler()
    main()
