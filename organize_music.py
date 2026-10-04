#!/usr/bin/env python3
"""
organize_music.py

Organize audio files into clean, media-server-friendly folder structures:
  Music/
  ├── Artist Name/
  │   ├── Album Name/
  │   │   ├── 01 - Track Title.mp3
  │   │   ├── 01 - Track Title.lrc
  │   │   └── 01 - Track Title.html
  │   └── folder.jpg (copied from Artist Art for Jellyfin / Plex)

Features:
  - Reads Artist, Album, Title, and Track from audio tags (MP3, FLAC, M4A, OGG, OPUS, WAV).
  - Falls back to filename parsing (Artist - Title) if tags are missing.
  - Moves all sidecar files (.lrc, .lrc.bak, .html, .txt, sidecar images) together with the audio file.
  - Copies artist pictures to folder.jpg in the artist's folder for Jellyfin / Plex support.
  - Safely handles name collisions and avoids overwriting existing files.
  - Cleans up empty source directories after moving.
  - Dry run mode (--dry-run) to preview changes before anything moves.

Usage:
  python3 organize_music.py                # uses organize.folders from config.toml
  python3 organize_music.py --dry-run      # preview only, moves nothing
  python3 organize_music.py "YouTube"      # organize specific folder
"""

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import (HERE, STOP, bold, cyan, dim, find_audio, folder_problem, green,
                    heading, install_stop_handler, load_config, log, progress,
                    red, require, resolve, section, start_log, yellow)

require("mutagen")
import mutagen
from mutagen import File as MutagenFile

from fix_album_art import AUDIO_EXTS, UNSUPPORTED_EXTS, clean_text
from fix_misidentified_tags import parse_filename

# Characters not allowed in folder or file names on macOS, Linux, and Windows
ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:\*\?"<>\|]')
WHITESPACE_RE = re.compile(r'\s+')
# Tags that say "nothing here" in words: treated as missing, never used as folder names
PLACEHOLDER_NAMES = {"unknown", "unknown album", "unknown artist", "null", "none", "n/a", "na", "nan", "<unknown>",
                     "untitled"}
SIDECAR_EXTS = {".lrc", ".bak", ".html", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".nfo"}
COMMON_IMAGES = {"folder.jpg", "cover.jpg", "artist.jpg", "album.jpg", "thumb.jpg"}
ALBUM_CACHE_FILE = HERE / "album_cache.json"

# Reject promotional sites, pirate watermarks, YouTube view counts, and scraper charts
JUNK_ALBUM_RE = re.compile(
    r"(?:https?://|www\.|\.(?:com|net|org|cn|ru|cc|to|xyz|me|io|top|site|tv|link|app)\b)"
    r"|(?:[【\[].*?(?:下载|无损|资源|xmwsyy|repack|flac|mp3).*?[】\]])"
    r"|(?:下载|无损音乐|視聴|views?\b|vistas?\b|visualizaciones\b|subscribe|bilibili|youtube|sound-?cloud|free download|album rip)"
    r"|(?:\b(?:billboard|hottest \d+|top \d+|chart|charts|shazam|various artists|compilation|greatest hits|now that[\x27\x27]s what i call|playlist|dj mix|best of|tik\s?tok)\b)"
    r"|(?:精选|精選|大全|神曲|ヒット|ランキング)",
    re.IGNORECASE,
)


def load_album_cache() -> dict:
    """Load cached track -> album mappings from disk."""
    if ALBUM_CACHE_FILE.is_file():
        try:
            with open(ALBUM_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            # an answer like "Unknown" or "null" from an older run is not an album: look those songs up again
            return {k: v for k, v in cache.items() if not (isinstance(v, str) and is_placeholder(v))}
        except Exception:
            return {}
    return {}


def save_album_cache(cache: dict) -> None:
    """Persist cached track -> album mappings to disk safely."""
    try:
        tmp = ALBUM_CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        tmp.replace(ALBUM_CACHE_FILE)
    except Exception:
        pass


def clean_query_title(title: str) -> str:
    """Strip bracketed noise, featuring artists, and remaster suffixes for search."""
    t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', str(title)).strip()
    t = re.sub(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$', '', t, flags=re.IGNORECASE).strip()
    return t or str(title)


def lookup_track_album(artist: str, title: str) -> Optional[str]:
    """
    Search LRCLIB and iTunes to find the official studio album for a loose track.
    Distinguishes true standalone singles from studio albums, rejecting watermarks,
    promotional sites, and YouTube view counts. Returns clean album name or None.
    """
    if not artist or not title:
        return None

    clean_t = clean_query_title(title)
    clean_a = re.sub(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$', '', str(artist), flags=re.IGNORECASE).strip() or artist

    # 1. Try LRCLIB API search (fast, rich multilingual catalog with full metadata)
    try:
        params = urllib.parse.urlencode({'track_name': clean_t, 'artist_name': clean_a})
        url = f"https://lrclib.net/api/search?{params}"
        req = urllib.request.Request(url, headers={'User-Agent': 'music-tools-organizer/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if isinstance(data, list) and data:
                raw_candidates = []
                for item in data[:15]:
                    alb = (item.get('albumName') or '').strip()
                    if not alb:
                        continue
                    low = alb.lower()
                    if low in ('unknown', 'unknown album', 'single', 'singles', 'none', 'null', 'nan'):
                        continue
                    if low.endswith(' - single') or low.endswith(' (single)'):
                        continue
                    if re.match(r'^\d{1,2}:\d{2}$', alb) or alb.isdigit():
                        continue
                    if JUNK_ALBUM_RE.search(alb):
                        continue
                    raw_candidates.append(alb)

                if raw_candidates:
                    clean_t_low = clean_t.lower()
                    non_title = []
                    title_matches = []
                    for alb in raw_candidates:
                        norm = re.sub(r'[\(\[\{].*?[\)\]\}]', '', alb).strip()
                        norm = re.sub(r'\s+EP\b', '', norm, flags=re.IGNORECASE).strip()
                        target = norm or alb
                        if target.lower() == clean_t_low:
                            title_matches.append((target, alb))
                        else:
                            non_title.append((target, alb))

                    # Prefer multi-track studio album if available
                    if non_title:
                        counts = Counter(t[0].lower() for t in non_title)
                        best_norm_low, _ = counts.most_common(1)[0]
                        for target, orig in non_title:
                            if target.lower() == best_norm_low:
                                return target

                    # If only title matches exist, check if any raw candidate indicated an EP/Soundtrack
                    for target, orig in title_matches:
                        if re.search(r'\b(?:EP|LP|Album|Deluxe|Edition|Soundtrack|OST)\b', orig, re.IGNORECASE) or re.search(r'[\(\[\{]', orig):
                            return target
    except Exception:
        pass

    # 2. Try iTunes Search API
    try:
        term = f"{clean_a} {clean_t}".strip()
        params = urllib.parse.urlencode({'term': term, 'entity': 'song', 'limit': 5})
        url = f"https://itunes.apple.com/search?{params}"
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            results = data.get('results', [])
            for r in results:
                album = (r.get('collectionName') or '').strip()
                track_count = r.get('trackCount', 0)
                if not album:
                    continue
                low_album = album.lower()
                if low_album.endswith(' - single') or low_album.endswith(' (single)') or (low_album.endswith(' - ep') and track_count <= 2):
                    continue
                if track_count <= 2 and clean_t.lower() in low_album:
                    continue
                if JUNK_ALBUM_RE.search(album):
                    continue
                return album
    except Exception:
        pass

    return None


def resolve_missing_albums(
    tracks_to_lookup: List[Tuple[str, str]],
    cache: dict,
    max_workers: int = 8,
    hide_progress: bool = False,
) -> dict:
    """Look up albums in parallel for distinct (artist, title) pairs not in cache."""
    needed = [(a, t) for a, t in set(tracks_to_lookup) if f"{a.lower()} // {clean_query_title(t).lower()}" not in cache]
    if not needed:
        return cache

    def worker(item):
        if STOP.is_set():
            return item, None
        a, t = item
        album = lookup_track_album(a, t)
        return item, album

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for (a, t), album in progress(pool.map(worker, needed), len(needed), "Finding albums for loose songs", hide_progress):
            if STOP.is_set():
                break
            key = f"{a.lower()} // {clean_query_title(t).lower()}"
            cache[key] = album or ""

    save_album_cache(cache)
    return cache


def sanitize_name(name: str, fallback: str = "Unknown") -> str:
    """Normalize and clean a directory or file name for filesystem safety."""
    if not name:
        return fallback
    text = unicodedata.normalize("NFKC", str(name)).strip()
    # Replace slashes and colons with sensible separators
    text = text.replace("/", "-").replace("\\", "-").replace(":", " - ")
    text = ILLEGAL_CHARS_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text).strip(" ._-")
    return text or fallback


def is_placeholder(text: str) -> bool:
    return text.strip().casefold() in PLACEHOLDER_NAMES


def read_track_meta(path: Path) -> dict:
    """Extract artist, album, title, and track number from mutagen tags or filename."""
    meta = {
        "artist": "",
        "album": "",
        "title": "",
        "track": None,
    }
    stem = clean_text(path.stem)
    try:
        f = MutagenFile(path, easy=True)
        if f and getattr(f, "tags", None):
            tags = f.tags
            albumartists = tags.get("albumartist") or tags.get("album_artist") or []
            artists = tags.get("artist") or []
            chosen_artist = albumartists[0] if albumartists else (artists[0] if artists else "")
            if chosen_artist:
                meta["artist"] = str(chosen_artist).strip()
            albums = tags.get("album") or []
            if albums:
                meta["album"] = str(albums[0]).strip()
            titles = tags.get("title") or []
            if titles:
                meta["title"] = str(titles[0]).strip()
            tracks = tags.get("tracknumber") or []
            if tracks:
                m = re.match(r"^(\d{1,3})", str(tracks[0]).strip())
                if m:
                    meta["track"] = m.group(1).zfill(2)
    except Exception:
        pass

    for key in ("artist", "album"):   # a tag that literally says "null" is a missing tag
        if is_placeholder(meta[key]):
            meta[key] = ""

    # Fallback parsing from filename if artist or title missing
    if not (meta["artist"] and meta["title"]):
        parsed = parse_filename(stem)
        if parsed.get("pattern") == "artist_dash_title":
            if not meta["artist"] and parsed.get("artist"):
                meta["artist"] = parsed["artist"]
            if not meta["title"] and parsed.get("title"):
                meta["title"] = parsed["title"]
        if not meta["track"] and parsed.get("track"):
            meta["track"] = str(parsed["track"]).zfill(2)

    if not meta["title"]:
        meta["title"] = stem

    return meta


def find_sidecars(audio_path: Path) -> List[Path]:
    """Find accompanying files (lyrics, html, images) that share the same stem."""
    stem = audio_path.stem
    parent = audio_path.parent
    sidecars = []
    try:
        for f in parent.iterdir():
            if f.is_file() and f != audio_path:
                if f.name.lower() in COMMON_IMAGES and stem.lower() not in COMMON_IMAGES:
                    continue
                # Starts with the same stem: song.lrc, song.html, song.lrc.bak, song.jpg
                if f.name == stem + f.suffix or f.name.startswith(stem + "."):
                    ext = f.suffix.lower()
                    if ext in SIDECAR_EXTS or (f.name.endswith(".lrc.bak")):
                        sidecars.append(f)
    except OSError:
        pass
    return sorted(sidecars)


@dataclass
class MovePlan:
    source_audio: Path
    dest_audio: Path
    sidecars: List[Tuple[Path, Path]] = field(default_factory=list)
    artist: str = ""
    album: str = ""
    title: str = ""
    is_noop: bool = False
    auto_album: bool = False
    error: str = ""


def match_existing_dir(parent: Path, name: str) -> Path:
    """If a subfolder with this name (or case-insensitive / Unicode NFKC equivalent) already exists, reuse it."""
    if parent.is_dir():
        try:
            norm_name = unicodedata.normalize("NFKC", name).lower()
            for entry in parent.iterdir():
                if entry.is_dir() and unicodedata.normalize("NFKC", entry.name).lower() == norm_name:
                    return entry
        except OSError:
            pass
    return parent / name


def match_existing_album_dir(artist_dir: Path, album_name: str) -> Path:
    """
    Find matching album subfolder inside artist_dir.
    1. Exact match (case-insensitive + Unicode NFKC).
    2. Base name match: e.g. 'OK Computer' matches existing 'OK Computer (Deluxe Edition)'
       or 'OK Computer (Collector's Edition)' matches existing 'OK Computer'.
    """
    if not artist_dir.is_dir() or not album_name:
        return artist_dir / album_name

    norm_target = unicodedata.normalize("NFKC", album_name).lower()
    base_target = re.sub(r'[\(\[\{].*?[\)\]\}]', '', norm_target).strip()
    base_target = re.sub(r'\s+(?:deluxe|expanded|remaster(?:ed)?|bonus(?:\s+tracks?)?)\b', '', base_target, flags=re.IGNORECASE).strip()

    fuzzy_match = None
    try:
        for entry in artist_dir.iterdir():
            if not entry.is_dir():
                continue
            entry_norm = unicodedata.normalize("NFKC", entry.name).lower()
            if entry_norm == norm_target:
                return entry  # Exact match!
            if base_target and len(base_target) >= 3:
                entry_base = re.sub(r'[\(\[\{].*?[\)\]\}]', '', entry_norm).strip()
                entry_base = re.sub(r'\s+(?:deluxe|expanded|remaster(?:ed)?|bonus(?:\s+tracks?)?)\b', '', entry_base, flags=re.IGNORECASE).strip()
                if entry_base == base_target:
                    fuzzy_match = entry
    except OSError:
        pass

    return fuzzy_match or (artist_dir / album_name)


def plan_move(
    audio_path: Path,
    music_dir: Path,
    fallback_artist: str = "Unknown Artist",
    fallback_album: str = "Singles",
    planned_dirs: Optional[Dict[str, str]] = None,
    planned_albums: Optional[Dict[str, Dict[str, str]]] = None,
    resolved_album: Optional[str] = None,
    track_meta: Optional[dict] = None,
) -> MovePlan:
    """Determine destination paths for the audio file and its sidecars."""
    meta = track_meta if track_meta is not None else read_track_meta(audio_path)
    raw_artist = meta["artist"] or fallback_artist
    raw_album = meta["album"]
    was_auto_album = False

    # Filter out single-track placeholder album names
    if not raw_album or raw_album.lower() in ("youtube", raw_artist.lower(), meta["title"].lower()) or is_placeholder(raw_album):
        if resolved_album and not is_placeholder(resolved_album):
            raw_album = resolved_album
            was_auto_album = True
        else:
            raw_album = fallback_album

    safe_artist = sanitize_name(raw_artist, fallback_artist)
    safe_album = sanitize_name(raw_album, fallback_album) if raw_album and raw_album.lower() not in ("none", "flat", "") else ""

    artist_dir = match_existing_dir(music_dir, safe_artist)
    if artist_dir.is_dir():
        safe_artist = artist_dir.name
    elif planned_dirs is not None:
        lower_a = unicodedata.normalize("NFKC", safe_artist).lower()
        if lower_a in planned_dirs:
            safe_artist = planned_dirs[lower_a]
            artist_dir = music_dir / safe_artist
        else:
            planned_dirs[lower_a] = safe_artist
    else:
        safe_artist = artist_dir.name

    if safe_album:
        dest_dir = match_existing_album_dir(artist_dir, safe_album)
        if dest_dir.is_dir():
            safe_album = dest_dir.name
        elif planned_albums is not None:
            norm_a = unicodedata.normalize("NFKC", safe_artist).lower()
            base_alb = re.sub(r'[\(\[\{].*?[\)\]\}]', '', unicodedata.normalize("NFKC", safe_album).lower()).strip()
            base_alb = re.sub(r'\s+(?:deluxe|expanded|remaster(?:ed)?|bonus(?:\s+tracks?)?)\b', '', base_alb, flags=re.IGNORECASE).strip() or safe_album.lower()
            artist_plans = planned_albums.setdefault(norm_a, {})
            if base_alb in artist_plans:
                safe_album = artist_plans[base_alb]
                dest_dir = artist_dir / safe_album
            else:
                artist_plans[base_alb] = safe_album
        else:
            safe_album = dest_dir.name
    else:
        dest_dir = artist_dir

    # Destination filename keeps the original audio filename
    dest_audio = dest_dir / audio_path.name
    try:
        is_noop = audio_path.samefile(dest_audio)
    except (OSError, ValueError):
        is_noop = (audio_path.resolve() == dest_audio.resolve()) if dest_audio.exists() and audio_path.exists() else (audio_path == dest_audio)

    # Collision resolution if a different file already exists at dest_audio
    if not is_noop and dest_audio.exists():
        try:
            if audio_path.samefile(dest_audio):
                is_noop = True
        except (OSError, ValueError):
            pass

    if not is_noop and dest_audio.exists():
        counter = 2
        stem = audio_path.stem
        ext = audio_path.suffix
        while dest_audio.exists():
            dest_audio = dest_dir / f"{stem} ({counter}){ext}"
            counter += 1

    # Plan sidecar moves to match the new destination stem
    sidecar_moves = []
    old_stem = audio_path.stem
    new_stem = dest_audio.stem
    for s in find_sidecars(audio_path):
        suffix_part = s.name[len(old_stem):]
        dest_sidecar = dest_dir / f"{new_stem}{suffix_part}"
        sidecar_moves.append((s, dest_sidecar))

    return MovePlan(
        source_audio=audio_path,
        dest_audio=dest_audio,
        sidecars=sidecar_moves,
        artist=safe_artist,
        album=safe_album,
        title=meta["title"],
        is_noop=is_noop,
        auto_album=was_auto_album,
    )


def execute_plan(plan: MovePlan, dry_run: bool = False) -> bool:
    """Execute the file movements in a plan. Returns True on success."""
    if plan.is_noop:
        return True
    if dry_run:
        return True

    dest_dir = plan.dest_audio.parent
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan.source_audio), str(plan.dest_audio))
        for src_s, dst_s in plan.sidecars:
            if src_s.exists():
                shutil.move(str(src_s), str(dst_s))
        return True
    except OSError as e:
        plan.error = str(e)
        return False


def copy_jellyfin_artist_art(music_dir: Path, artist_name: str, artist_art_dir: Path, dry_run: bool = False) -> bool:
    """Ensure <music_dir>/<Artist>/folder.jpg exists by copying from Artist Art."""
    artist_dir = match_existing_dir(music_dir, artist_name)
    if not dry_run and not artist_dir.is_dir():
        return False
    target_folder_jpg = artist_dir / "folder.jpg"
    target_artist_jpg = artist_dir / "artist.jpg"
    if target_folder_jpg.exists() or target_artist_jpg.exists():
        return False

    # Look for matching picture in artist_art_dir
    candidates = [
        artist_art_dir / f"{artist_name}.jpg",
        artist_art_dir / f"{artist_name}.png",
        artist_art_dir / f"{artist_name}.jpeg",
    ]
    matched_cand = None
    for cand in candidates:
        if cand.is_file():
            matched_cand = cand
            break
    if not matched_cand and artist_art_dir.is_dir():
        lower_name = artist_name.lower()
        try:
            for f in artist_art_dir.iterdir():
                if f.is_file() and not f.name.startswith("._"):
                    if f.stem.lower() == lower_name and f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                        matched_cand = f
                        break
        except OSError:
            pass

    if matched_cand:
        if not dry_run:
            try:
                artist_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(matched_cand), str(target_folder_jpg))
            except OSError:
                return False
        return True
    return False


def clean_empty_directories(folders: List[Path], root_dir: Path) -> int:
    """Remove empty source directories up to root_dir, ignoring .DS_Store."""
    removed = 0
    resolved_root = root_dir.resolve()
    # Sort paths deepest first
    dirs_to_check = sorted(set(folders), key=lambda p: len(p.parts), reverse=True)
    for d in dirs_to_check:
        curr = d.resolve()
        while curr != resolved_root and curr.is_relative_to(resolved_root):
            try:
                entries = list(curr.iterdir())
                # If only .DS_Store or Thumbs.db, remove them
                junk = [e for e in entries if e.name in (".DS_Store", "Thumbs.db")]
                if len(junk) == len(entries):
                    for j in junk:
                        j.unlink(missing_ok=True)
                    curr.rmdir()
                    removed += 1
                    curr = curr.parent
                else:
                    break
            except OSError:
                break
    return removed


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    install_stop_handler()

    parser = argparse.ArgumentParser(
        prog="organize_music.py",
        description="Organize audio files into Artist/Album/ folders with accompanying lyrics and artist photos.",
    )
    parser.add_argument("paths", nargs="*", help="Folders or files to organize (default: organize.folders in config)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be moved without moving anything")
    parser.add_argument("--fallback-artist", help="Folder name when artist cannot be found (default: Unknown Artist)")
    parser.add_argument("--fallback-album", help="Folder name when album cannot be found (default: Singles; '' for flat)")
    parser.add_argument("--no-auto-album", action="store_true", help="Do not search online for missing album names")
    parser.add_argument("--no-artist-art", action="store_true", help="Do not copy artist picture to folder.jpg")
    parser.add_argument("--no-clean", action="store_true", help="Do not clean empty source directories")
    parser.add_argument("--no-progress", action="store_true", help="Hide progress bar")
    parser.add_argument("--config", help="Path to config.toml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    music_dir = Path(cfg["music_dir"]).expanduser().resolve()
    org_cfg = cfg.get("organize", {})
    artist_art_cfg = cfg.get("artist_art", {})

    fallback_artist = args.fallback_artist or org_cfg.get("fallback_artist", "Unknown Artist")
    fallback_album = args.fallback_album if args.fallback_album is not None else org_cfg.get("fallback_album", "Singles")
    auto_album = not args.no_auto_album and org_cfg.get("auto_album", True)
    copy_artist_art = not args.no_artist_art and org_cfg.get("copy_artist_art", True)
    clean_empty = not args.no_clean and org_cfg.get("clean_empty_dirs", True)
    workers = int(cfg.get("workers", 8))

    artist_art_dir = resolve(artist_art_cfg.get("output_dir", "Artist Art"), str(music_dir))

    # Determine folders to scan
    target_paths = args.paths or org_cfg.get("folders", ["."])
    scan_paths = [resolve(p, str(music_dir)) for p in target_paths]

    for p in scan_paths:
        problem = folder_problem(p)
        if problem:
            sys.exit(f"{red('✗')} Cannot read {p}: {problem}")

    start_log("organize", cfg)

    heading("Organize library into Artist/Album folders")
    if args.dry_run:
        log(yellow("Preview only (dry run): no files will be moved.\n"))

    all_exts = AUDIO_EXTS | UNSUPPORTED_EXTS
    files = list(find_audio(scan_paths, all_exts))
    if not files:
        log("No audio files found to organize.")
        return 0

    log(f"Scanning {len(files)} files...\n")
    parsed_files: List[Tuple[Path, dict]] = []
    tracks_needing_album: List[Tuple[str, str]] = []

    for path in progress(files, len(files), "Scanning tags", args.no_progress):
        if STOP.is_set():
            log(yellow("\nStopped cleanly."))
            return 1
        meta = read_track_meta(path)
        raw_album = meta["album"]
        raw_artist = meta["artist"] or fallback_artist
        if not raw_album or raw_album.lower() in ("youtube", raw_artist.lower(), meta["title"].lower()) or is_placeholder(raw_album):
            if raw_artist and meta["title"] and raw_artist != fallback_artist:
                tracks_needing_album.append((raw_artist, meta["title"]))
        parsed_files.append((path, meta))

    album_cache = load_album_cache() if auto_album else {}
    if auto_album and tracks_needing_album:
        resolve_missing_albums(tracks_needing_album, album_cache, max_workers=workers, hide_progress=args.no_progress)

    plans: List[MovePlan] = []
    source_dirs = set()
    planned_dirs: Dict[str, str] = {}
    planned_albums: Dict[str, Dict[str, str]] = {}

    for path, meta in progress(parsed_files, len(parsed_files), "Checking destinations", args.no_progress):
        if STOP.is_set():
            log(yellow("\nStopped cleanly."))
            return 1
        raw_artist = meta["artist"] or fallback_artist
        cache_key = f"{raw_artist.lower()} // {clean_query_title(meta['title']).lower()}"
        found_album = album_cache.get(cache_key) if auto_album else None
        plan = plan_move(
            path,
            music_dir,
            fallback_artist=fallback_artist,
            fallback_album=fallback_album,
            planned_dirs=planned_dirs,
            planned_albums=planned_albums,
            resolved_album=found_album,
            track_meta=meta,
        )
        plans.append(plan)
        if not plan.is_noop:
            source_dirs.add(path.parent)

    moved_count = 0
    noop_count = 0
    err_count = 0
    artists_seen = set()

    for plan in progress(plans, len(plans), "Organizing files", args.no_progress):
        if STOP.is_set():
            log(yellow("\nStopped cleanly."))
            return 1

        if plan.is_noop:
            noop_count += 1
            continue

        artists_seen.add(plan.artist)
        rel_src = str(plan.source_audio.relative_to(music_dir)) if plan.source_audio.is_relative_to(music_dir) else str(plan.source_audio)
        rel_dst = str(plan.dest_audio.relative_to(music_dir)) if plan.dest_audio.is_relative_to(music_dir) else str(plan.dest_audio)
        album_tag = f" {green('[album: ' + plan.album + ']')}" if plan.auto_album else ""

        if args.dry_run:
            log(f"  {cyan('→')} {rel_src} {dim('→')} {green(rel_dst)}{album_tag}")
            for src_s, dst_s in plan.sidecars:
                log(f"    {dim('+ sidecar:')} {src_s.name} {dim('→')} {dst_s.name}")
            moved_count += 1
            continue

        ok = execute_plan(plan, dry_run=False)
        if ok:
            moved_count += 1
            log(f"  {green('✓')} {rel_src} {dim('→')} {rel_dst}{album_tag}")
            for src_s, dst_s in plan.sidecars:
                log(f"    {dim('+ moved sidecar:')} {dst_s.name}")
        else:
            err_count += 1
            log(f"  {red('✗')} {rel_src}: {plan.error}")

    # Copy artist pictures for Jellyfin
    art_copied = 0
    if copy_artist_art and artists_seen:
        unique_artists = {}
        for a in artists_seen:
            unique_artists.setdefault(a.lower(), a)
        for artist in sorted(unique_artists.values(), key=lambda s: s.lower()):
            if copy_jellyfin_artist_art(music_dir, artist, artist_art_dir, dry_run=args.dry_run):
                art_copied += 1
                prefix = yellow("[dry-run] Would copy") if args.dry_run else green("Copied")
                log(f"  {dim('♫')} {prefix} artist photo {dim('→')} {artist}/folder.jpg (for Jellyfin)")

    # Clean empty directories
    cleaned_count = 0
    if clean_empty and source_dirs and not args.dry_run:
        cleaned_count = clean_empty_directories(list(source_dirs), music_dir)

    auto_album_count = sum(1 for p in plans if p.auto_album)

    log("\n" + bold("Summary:"))
    action = "Would move" if args.dry_run else "Moved"
    log(f"  {green(action)}: {moved_count} songs (and companion lyrics/images)")
    log(f"  {dim('Already in place')}: {noop_count} songs")
    if auto_album_count:
        log(f"  {green('Albums detected')}: {auto_album_count} loose songs matched to official albums")
    if art_copied:
        log(f"  {green('Artist photos')}: {art_copied} folder.jpg set up for Jellyfin")
    if cleaned_count:
        log(f"  {dim('Cleaned')}: {cleaned_count} empty directories removed")
    if err_count:
        log(f"  {red('Errors')}: {err_count} files failed to move")

    return 1 if err_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
