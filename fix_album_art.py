#!/usr/bin/env python3
"""
Find songs with no embedded cover art and add art from YouTube.

For each audio file missing art, the image source is chosen in this order:
  1. A sidecar image next to the song with the same name (song.jpg / .png / .webp)
  2. The YouTube video ID already in the file (filename "[dQw4w9WgXcQ]" or a
     youtube URL in the comment / purl / description tags) -> that video's thumbnail
  3. A YouTube search for "<artist> <title>" (or the filename). A result is used
     automatically only if its title matches exactly; otherwise you get to pick
     one, paste a link, or skip (asked after the search finishes).

Searches and downloads run in parallel. The 16:9 thumbnail is center-cropped
to a square unless crop = false.

Usage:
  python3 fix_album_art.py                     # folders from config.toml
  python3 fix_album_art.py --list-missing      # only list songs without art
  python3 fix_album_art.py --dry-run           # find art but don't write it
  python3 fix_album_art.py "song.flac" --force # redo one song
  python3 fix_album_art.py "song.flac" --url https://youtu.be/XXXXXXXXXXX
  python3 fix_album_art.py --retry             # re-run only the songs that failed last time
  python3 fix_album_art.py /some/folder --auto # never ask, skip unsure matches
"""

import argparse
import base64
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from common import (HERE, STOP, fetch_url, find_audio, start_log, install_stop_handler, load_config, log,
                    normal_ctrl_c, progress, require, resolve)

require("mutagen", "PIL")

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus"}
UNSUPPORTED_EXTS = {".webm", ".wav", ".aac"}
IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp"]
FAILED_LIST = HERE / "failed_album_art.txt"
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|\[)([A-Za-z0-9_-]{11})(?:\]|\b)")
BARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
NOISE_RE = re.compile(
    r"\[[A-Za-z0-9_-]{11}\]|\((?:official|lyric|lyrics|audio|video|music video|visualizer|hd|4k|explicit)[^)]*\)",
    re.IGNORECASE,
)
# yt-dlp swaps characters that aren't allowed in filenames for look-alikes.
# Undo that, or YouTube search finds nothing.
FILENAME_LOOKALIKES = str.maketrans({
    "⧸": "/", "⧹": "\\", "｜": "|", "？": "?", "：": ":", "＊": "*",
    "＂": '"', "＜": "<", "＞": ">", "​": "", "‌": "", "‍": "", "﻿": "",
})


@dataclass
class Job:
    path: Path
    query: str = ""
    video_id: str = ""
    source: str = ""
    image: bytes = b""
    candidates: list = field(default_factory=list)
    status: str = "pending"  # pending | ready | unsure | failed | done


# ---------------------------------------------------------------- text matching

def clean_text(s: str) -> str:
    """NFC (macOS stores filenames decomposed) and undo yt-dlp's look-alike characters."""
    return unicodedata.normalize("NFC", s).translate(FILENAME_LOOKALIKES)


def word_list(s: str) -> list:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", s).casefold())


def match_score(query: str, title: str) -> float:
    """1.0 only when the title has exactly the same words as the query."""
    q, t = word_list(query), word_list(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    qs, ts = set(q), set(t)
    return min(len(qs & ts) / len(qs | ts), 0.99)


# ---------------------------------------------------------------- tags

def has_art(path: Path) -> bool:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                return bool(ID3(path).getall("APIC"))
            except ID3NoHeaderError:
                return False
        if ext in (".m4a", ".mp4"):
            tags = MP4(path).tags
            return bool(tags and tags.get("covr"))
        if ext == ".flac":
            return bool(FLAC(path).pictures)
        if ext in (".ogg", ".opus"):
            f = MutagenFile(path)
            return bool(f and f.tags and f.tags.get("metadata_block_picture"))
    except Exception as e:
        log(f"! {path.name}: could not read tags: {e}")
    return False


def read_tags(path: Path, keep_file: bool = False) -> dict:
    """Return title/artist/duration plus any free text that might hold a YouTube URL. Reads the file
    once, so callers that also need the length don't have to parse it again themselves. With
    `keep_file`, the parsed mutagen object comes back as "file" too (None if unreadable), for callers
    that want to look at more tags without opening the file a second time."""
    out = {"title": "", "artist": "", "text": "", "duration": None, "file": None}
    try:
        f = MutagenFile(path)
    except Exception:
        return out
    if keep_file:
        out["file"] = f
    if f and getattr(f, "info", None) is not None:
        try:
            out["duration"] = float(f.info.length)
        except (TypeError, ValueError, AttributeError):
            pass
    if not f or not f.tags:
        return out
    t, texts = f.tags, []
    ext = path.suffix.lower()
    if ext == ".mp3":
        out["title"] = str(t.get("TIT2", "") or "")
        out["artist"] = str(t.get("TPE1", "") or "")
        texts = [str(getattr(fr, "url", "") or fr) for k, fr in t.items() if not k.startswith("APIC")]
    elif ext in (".m4a", ".mp4"):
        out["title"] = (t.get("\xa9nam") or [""])[0]
        out["artist"] = (t.get("\xa9ART") or [""])[0]
        for k in ("\xa9cmt", "desc", "ldes", "purl"):
            texts += [str(v) for v in t.get(k, [])]
    else:
        out["title"] = (t.get("title") or [""])[0]
        out["artist"] = (t.get("artist") or [""])[0]
        for k in ("comment", "description", "purl", "url"):
            texts += t.get(k, [])
    out["text"] = " ".join(texts)
    return out


def embed(path: Path, jpeg: bytes):
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        tags.save(path, v2_version=3)
    elif ext in (".m4a", ".mp4"):
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        f.tags["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    else:
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", jpeg
        pic.width, pic.height = Image.open(io.BytesIO(jpeg)).size
        pic.depth = 24
        if ext == ".flac":
            f = FLAC(path)
            f.clear_pictures()
            f.add_picture(pic)
        else:
            f = MutagenFile(path)  # .ogg may hold Vorbis, Opus or FLAC audio
            if f is None or not isinstance(f, (OggOpus, OggVorbis, OggFLAC)):
                raise ValueError("not a recognised Ogg file")
            if f.tags is None:
                f.add_tags()
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()


# ---------------------------------------------------------------- image sources

def youtube_search(query: str, n: int):
    """Return (results, error message)."""
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp is not installed (brew install yt-dlp)"
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-warnings", "--flat-playlist", "-J", f"ytsearch{n}:{query}"],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return [], "YouTube search timed out"
    except OSError as e:
        return [], f"could not run yt-dlp: {e}"
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        return [], f"YouTube search failed: {err[:150]}"
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except (json.JSONDecodeError, AttributeError):
        return [], "YouTube search returned something unexpected"
    return [e for e in entries if isinstance(e, dict) and e.get("id")], ""


def search_queries(path: Path, tags: dict) -> list:
    """
    Searches to try, best first: the song's tags, then the filename, then both
    with the symbols stripped. Tags can be wrong (e.g. mis-tagged by Picard),
    so the filename is always tried too.
    """
    def tidy(text):
        return re.sub(r"\s+", " ", NOISE_RE.sub("", clean_text(text))).strip(" -_")

    raw = []
    if tags["title"] and tags["artist"]:
        raw.append(tidy(f'{tags["artist"]} {tags["title"]}'))
    raw.append(tidy(path.stem))
    if tags["title"] and not tags["artist"]:
        raw.append(tidy(tags["title"]))
    raw += [" ".join(word_list(q)) for q in list(raw)]
    queries, seen = [], set()
    for q in raw:
        if q and q.casefold() not in seen:
            seen.add(q.casefold())
            queries.append(q)
    return queries


def download_thumbnail(video_id: str) -> bytes:
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            data = fetch_url(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg", timeout=20)
            # YouTube serves a 120x90 placeholder when a size doesn't exist
            if Image.open(io.BytesIO(data)).width > 120:
                return data
        except Exception:
            continue
    return b""


def to_jpeg(data: bytes, crop: bool, quality: int) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if crop and img.width != img.height:
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def resolve_job(job: Job, opts: dict) -> Job:
    """Find an image for one song. Runs in a worker thread, never prompts or raises."""
    if STOP.is_set():
        job.status, job.source = "failed", "stopped before searching"
        return job
    try:
        return _resolve_job(job, opts)
    except Exception as e:
        job.status, job.source = "failed", f"unexpected error: {e}"
        return job


def _resolve_job(job: Job, opts: dict) -> Job:
    path = job.path
    if opts["sidecar_images"] and not job.video_id:
        for ext in IMAGE_EXTS:
            side = path.with_suffix(ext)
            if side.is_file():
                try:
                    job.image = side.read_bytes()
                    Image.open(io.BytesIO(job.image)).verify()
                except Exception:
                    log(f"! {side.name} isn't a readable image, ignoring it")
                    job.image = b""
                    continue
                job.source, job.status = f"sidecar {side.name}", "ready"
                return job

    tags = read_tags(path)
    for text in (() if job.video_id else (path.stem, tags["text"])):
        m = YT_ID_RE.search(text)
        if m:
            job.video_id, job.source = m.group(1), f"video id {m.group(1)} from file"
            break

    if not job.video_id:
        queries = search_queries(path, tags)
        if not queries:
            job.status, job.source = "failed", "nothing to search for"
            return job
        seen, pool, error = set(), [], ""
        for query in queries:
            if STOP.is_set():
                break
            results, error = youtube_search(query, opts["search_results"])
            for e in results:
                score = match_score(query, e.get("title", ""))
                # a one-word search ("Guts") matches too many unrelated videos to trust
                if score >= opts["min_match"] and len(word_list(query)) >= 2:
                    job.query, job.video_id = query, e["id"]
                    job.source = f'search "{query}" -> {e.get("title", "")} ({score:.0%} match)'
                    break
                if e["id"] not in seen:
                    seen.add(e["id"])
                    pool.append((score, query, e))
            if job.video_id:
                break
        if not job.video_id:
            if not pool:
                job.query = queries[0]
                job.status, job.source = "failed", error or f"no search results for {len(queries)} different searches"
                return job
            # nothing exact: offer the closest results from all searches
            pool.sort(key=lambda t: -t[0])
            job.query = pool[0][1]
            job.candidates = [e for _, _, e in pool[: opts["search_results"] + 3]]
            job.status = "unsure"
            return job

    job.image = download_thumbnail(job.video_id)
    job.status = "ready" if job.image else "failed"
    if not job.image:
        job.source = f"could not download thumbnail for {job.video_id} (offline?)"
    return job


def review(job: Job) -> None:
    """Ask the user to choose for a song without an exact match."""
    print(f"\n? {job.path.name}")
    print(f'  no exact match for "{job.query}". Top results:')
    for i, e in enumerate(job.candidates, 1):
        print(f'    {i}. {e.get("title", "")}  [{e.get("channel") or e.get("uploader") or ""}]')
    while True:
        ans = input(f"  pick 1-{len(job.candidates)}, paste a YouTube URL/ID, or Enter to skip: ").strip()
        if not ans:
            job.status, job.source = "failed", "skipped by you"
            return
        if ans.isdigit() and 1 <= int(ans) <= len(job.candidates):
            e = job.candidates[int(ans) - 1]
            job.video_id, job.source = e["id"], f'you picked -> {e.get("title", "")}'
            break
        m = YT_ID_RE.search(ans) or BARE_ID_RE.fullmatch(ans)
        if m:
            job.video_id = m.group(1) if m.re is YT_ID_RE else m.group(0)
            job.source = f"you pasted {job.video_id}"
            break
        print("  didn't understand that")
    job.image = download_thumbnail(job.video_id)
    job.status = "ready" if job.image else "failed"
    if not job.image:
        job.source = f"could not download thumbnail for {job.video_id} (offline?)"


# ---------------------------------------------------------------- main

def _probe(path: Path, entry: str) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", entry,
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=60)
    return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""


def move_to_trash(path: Path) -> bool:
    """Move a file to the Trash (recoverable). Returns False if that isn't possible."""
    if sys.platform != "darwin":
        return False
    script = 'on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv))\nend run'
    r = subprocess.run(["osascript", "-e", script, str(path)], capture_output=True, timeout=60)
    return r.returncode == 0 and not path.exists()


def convert_webm(path: Path):
    """
    Copy a .webm's audio into an Ogg file (.opus or .ogg) without re-encoding,
    check it, then move the .webm to the Trash. Returns (new_path or None, message).
    """
    if STOP.is_set():
        return None, "stopped before converting"
    try:
        codec = _probe(path, "stream=codec_name")
        ext = {"opus": ".opus", "vorbis": ".ogg"}.get(codec)
        if not ext:
            if not codec:
                return None, "file is corrupted or unreadable (invalid container/audio stream)"
            return None, f"audio is {codec}, can't convert without re-encoding"
        out = path.with_suffix(ext)
        if out.exists():
            return None, f"{out.name} already exists, not overwriting it"
        tmp = out.with_name(f".{out.stem}.converting{ext}")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-c:a", "copy",
                            "-map_metadata", "0", str(tmp)], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return None, f"ffmpeg failed: {(r.stderr.strip().splitlines() or ['?'])[-1][:150]}"
        # make sure the copy is complete before touching the original
        new = MutagenFile(tmp)
        src_len = float(_probe(path, "format=duration") or 0)
        if new is None or (src_len and abs(new.info.length - src_len) > 2):
            tmp.unlink(missing_ok=True)
            return None, "converted file didn't check out, original kept"
        os.replace(tmp, out)
        if move_to_trash(path):
            return out, f"converted to {out.name} (original .webm moved to the Trash)"
        return out, f"converted to {out.name} (original .webm kept; delete it yourself)"
    except Exception as e:
        return None, f"conversion failed: {e}"


def search_link(job: Job) -> str:
    q = job.query or clean_text(job.path.stem)
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/folders (default: album_art.folders from the config)")
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--list-missing", action="store_true", help="only list songs without art")
    ap.add_argument("--dry-run", action="store_true", help="find art but don't write it")
    ap.add_argument("--force", action="store_true", default=None, help="replace art that's already there")
    ap.add_argument("--url", help="use this YouTube link/ID for the given song (implies --force)")
    ap.add_argument("--retry", action="store_true", help=f"re-run the songs listed in {FAILED_LIST.name}")
    ap.add_argument("--convert-webm", action="store_true", default=None,
                    help="convert .webm files to .opus (no quality loss) so they can hold art")
    ap.add_argument("--auto", action="store_true", help="never ask; skip songs without an exact match")
    ap.add_argument("--no-crop", action="store_true", help="keep 16:9 thumbnails instead of cropping square")
    ap.add_argument("--min-match", type=float, help="0-1, how closely a title must match to auto-accept (default 1.0)")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel searches/downloads")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start_log("album-art", cfg)
    opts = dict(cfg["album_art"])
    if args.force:
        opts["force"] = True
    if args.convert_webm:
        opts["convert_webm"] = True
    if args.no_crop:
        opts["crop"] = False
    if args.min_match is not None:
        if not 0 <= args.min_match <= 1:
            sys.exit("--min-match must be between 0 and 1")
        opts["min_match"] = args.min_match
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress

    if args.retry:
        if not FAILED_LIST.exists():
            sys.exit(f"Nothing to retry ({FAILED_LIST.name} not found).")
        args.paths = [l for l in FAILED_LIST.read_text().splitlines() if l.strip()]
    url_id = None
    if args.url:
        m = YT_ID_RE.search(args.url) or BARE_ID_RE.fullmatch(args.url.strip())
        if not m or len(args.paths) != 1:
            sys.exit("--url needs exactly one song path and a YouTube link or 11-character video ID.")
        url_id = m.group(1) if m.re is YT_ID_RE else m.group(0)
        opts["force"] = True

    paths = args.paths or [resolve(f, cfg["music_dir"]) for f in opts["folders"]]
    if cfg.get("_path"):
        log(f"Using config {cfg['_path']}")
    files = list(find_audio(paths, AUDIO_EXTS | UNSUPPORTED_EXTS))
    unsupported = {p: f"{p.suffix} can't hold cover art (use --convert-webm)" if p.suffix.lower() == ".webm"
                   else f"{p.suffix} can't hold cover art (convert to m4a/mp3/flac first)"
                   for p in files if p.suffix.lower() in UNSUPPORTED_EXTS}
    files = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
    log(f"Found {len(files)} songs in {len(paths)} location(s).")

    webms = [p for p in unsupported if p.suffix.lower() == ".webm"]
    if opts["convert_webm"] and webms and not args.list_missing and not args.dry_run:
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            log("! --convert-webm needs ffmpeg (brew install ffmpeg); skipping conversion")
        else:
            with ThreadPoolExecutor(min(workers, 4)) as pool:
                converted = list(progress(pool.map(convert_webm, webms), len(webms), "Converting webm", quiet))
            for src, (new, msg) in zip(webms, converted):
                log(f"{'↻' if new else '✗'} {src.name}: {msg}")
                if new:
                    del unsupported[src]
                    files.append(new)
                else:
                    unsupported[src] = msg
    with ThreadPoolExecutor(workers) as pool:
        # 1. which songs need art
        if opts["force"] and not args.list_missing:
            missing = files
        else:
            flags = list(progress(pool.map(has_art, files), len(files), "Checking art", quiet))
            missing = [p for p, ok in zip(files, flags) if not ok]
        log(f"{len(files) - len(missing)} already have art, {len(missing)} need it.")

        if args.list_missing:
            for p in missing:
                log(f"  {p}")
            for p, why in unsupported.items():
                log(f"  {p}  ({why})")
            return

        # 2. search + download in parallel
        jobs = [Job(p, video_id=url_id or "", source=f"you gave {url_id}" if url_id else "") for p in missing]
        jobs = list(progress(pool.map(lambda j: resolve_job(j, opts), jobs), len(jobs), "Finding art", quiet))

    # 3. ask about unsure matches (after the bars, so prompts aren't interleaved)
    unsure = [j for j in jobs if j.status == "unsure"]
    if unsure:
        if args.auto or not sys.stdin.isatty():
            for j in unsure:
                j.status, j.source = "failed", "no exact match (use without --auto to choose)"
        elif not STOP.is_set():
            print(f"\n{len(unsure)} song(s) need you to pick a match (Ctrl-C to skip the rest):")
            try:
                with normal_ctrl_c():
                    for j in unsure:
                        review(j)
            except (KeyboardInterrupt, EOFError):
                print("\nSkipping the rest.")
        for j in unsure:
            if j.status == "unsure":
                j.status, j.source = "failed", "skipped"

    # 4. write
    ready = [j for j in jobs if j.status == "ready"]
    if not args.dry_run and ready:
        def write(j: Job):
            if STOP.is_set():
                j.status, j.source = "failed", "stopped before writing"
                return j
            try:
                embed(j.path, to_jpeg(j.image, opts["crop"], opts["jpeg_quality"]))
                j.status = "done"
            except Exception as e:
                j.status, j.source = "failed", f"embed failed: {e}"
            return j
        with ThreadPoolExecutor(min(workers, 4)) as pool:  # disk-bound; don't thrash the drive
            list(progress(pool.map(write, ready), len(ready), "Adding art", quiet))

    # 5. report
    ok = [j for j in jobs if j.status in ("done", "ready")]
    failed = [j for j in jobs if j.status == "failed"]
    print()
    for j in ok:
        link = f"  https://youtu.be/{j.video_id}" if j.video_id else ""
        print(f"✓ {j.path.name}\n    {j.source}{link}")
    script = shlex.quote(str(Path(__file__).resolve()))
    if failed or unsupported:
        print("\nStill without art:")
        for j in failed:
            print(f"✗ {j.path.name}\n    reason: {j.source or 'no match'}")
            print(f"    search: {search_link(j)}")
            print(f"    fix:    python3 {script} {shlex.quote(str(j.path))} --url PASTE_LINK")
        for p, why in unsupported.items():
            print(f"✗ {p.name}\n    {why}")
    if (failed or unsupported) and not args.dry_run:
        FAILED_LIST.write_text("".join(f"{p}\n" for p in [j.path for j in failed] + list(unsupported)))
        print(f"\nRe-run just these with:  python3 {script} --retry")
    elif not failed and not unsupported and args.retry and FAILED_LIST.exists():
        FAILED_LIST.unlink()
    print(f"\nDone. already had art: {len(files) - len(missing)}, "
          f"{'would add' if args.dry_run else 'added'}: {len(ok)}, "
          f"without art: {len(failed) + len(unsupported)}")


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
