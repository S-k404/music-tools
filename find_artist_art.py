#!/usr/bin/env python3
"""
Find a picture for every artist in your music library.

Artists come from each song's Artist tag (or, for songs without one, the
filename: "Artist - Title"). "A feat. B" and "A; B" count as two artists.
For each artist without a picture yet, the picture is downloaded from Deezer
(no account or key needed) and saved as "<Artist>.jpg" in one folder:
output_dir in config.toml, "Artist Art" inside your music folder by default.
Your songs are never changed.

An artist is used automatically only when Deezer has an artist with exactly
the same name (the most popular one if several do). Deezer doesn't have
everyone, so an artist it doesn't know (or has no picture for) is looked up
on YouTube too, using their channel picture. Otherwise, once all lookups are
done, you're shown the closest artists and can pick one, paste a link, or
skip. Run it again any time: artists that already have a picture are
skipped, so a second run only retries the ones that failed.

Usage:
  python3 find_artist_art.py                       # artist folders from config.toml
  python3 find_artist_art.py --list-missing        # list artists without a picture (no downloading)
  python3 find_artist_art.py --dry-run             # look artists up, save nothing
  python3 find_artist_art.py /some/folder --auto   # never ask, skip unsure artists
  python3 find_artist_art.py --artist "Radiohead" --artist "Fred again.."
  python3 find_artist_art.py --artist "Some DJ" --image https://example.com/photo.jpg
"""

import argparse
import io
import json
import os
import queue
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from common import (STOP, atomic_write, bold, cyan, dim, fetch_url, find_audio, fit, folder_problem, green, heading, install_stop_handler,
                    load_config, log, normal_ctrl_c, pink, plural, progress, red, require, resolve, section, start_log,
                    text_width, yellow)

require("mutagen", "PIL")

from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags
from PIL import Image

from fix_album_art import AUDIO_EXTS, IMAGE_EXTS, UNSUPPORTED_EXTS, clean_text, match_score, word_list
from fix_misidentified_tags import parse_filename

PROG = os.environ.get("MUSIC_TOOLS_NAME") or "mt"  # how the command is typed in the hints
API = "https://api.deezer.com"
USER_AGENT = "music-tools (artist pictures)"
MAX_IMAGE_BYTES = 20_000_000
# Tags that mean "we don't know", not a real artist
NOT_AN_ARTIST = {"various artists", "various", "va", "unknown", "unknown artist", "artist", "n a", "none"}
FEAT_RE = re.compile(r"\s*;\s*|\x00|\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
# "A & B", "A x B", "A with B", "A as B" (an alias), "A / B", "A、B", "A feat.B", "A vo. B"
COMBO_RE = re.compile(r"\s+(?:&|and|x|\+|vs\.?|with|as)\s+|\s*[&＆×/、]\s*|,\s+|\s+(?:feat|ft|featuring|vo)\.\s*"
                      r"|\s+(?:feat|ft|featuring)\s+", re.IGNORECASE)
# Words that show up as "artists" when a title or genre ends up in the artist tag ("KPOP, House, ZARA")
GENRE_WORDS = {"house", "kpop", "k pop", "jpop", "j pop", "pop", "rock", "hip hop", "hiphop", "rap", "edm", "remix",
               "mix", "music", "lofi", "lo fi", "phonk", "techno", "trap", "dnb", "drill", "anime", "ost", "dj"}
TOPIC_RE = re.compile(r"\s+-\s+Topic$", re.IGNORECASE)   # YouTube's auto-generated channel names
DEEZER_ARTIST_RE = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?artist/(\d+)")
# Deezer serves an empty-hash URL when an artist has no picture
NO_PICTURE_MARKERS = ("/artist//", "d41d8cd98f00b204e9800998ecf8427e")
# A YouTube search URL restricted to channels ("&sp=..." is the "Channel" filter)
YOUTUBE_CHANNEL_SEARCH = "https://www.youtube.com/results?search_query={}&sp=EgIQAg%253D%253D"
# The same search restricted to videos, sorted by view count
YOUTUBE_VIDEO_SEARCH = "https://www.youtube.com/results?search_query={}&sp=CAMSAhAB"
# One page of results is plenty. Without a limit yt-dlp pages through every result
# YouTube has, which takes tens of seconds per artist and gets us rate-limited (HTTP 403).
YOUTUBE_RESULTS = 10
YOUTUBE_TIMEOUT = 25
# YouTube rate-limits bursts of searches, so ask slowly, don't sit through yt-dlp's
# retry backoff when it refuses, and stop asking once it refuses over and over.
# An artist we give up on is reported as not found, and a later run retries them.
YOUTUBE_SEARCHES_PER_SECOND = 3
YOUTUBE_RETRIES = 1
YOUTUBE_GIVE_UP_AFTER = 5
YOUTUBE_AVATAR_SIZE_RE = re.compile(r"=s\d+-")
TOPIC_CHANNEL_RE = re.compile(r"\s*-\s*Topic$")  # YouTube's auto-generated "artist's music" channel


@dataclass
class Pick:
    """One Deezer artist (or custom image) chosen for a name."""
    name: str                # the name the picture is saved under
    deezer_name: str = ""
    fans: int = 0
    link: str = ""
    picture_url: str = ""
    image: bytes = b""
    saved: str = ""          # "", "saved", "exists" or "error: ..."


@dataclass
class Job:
    name: str
    songs: int = 0
    picks: list = field(default_factory=list)
    candidates: list = field(default_factory=list)   # Deezer results to choose from
    status: str = "pending"  # pending | found | ready | unsure | failed | done
    reason: str = ""
    by_views: bool = False   # the candidates are channels of this name's most-watched videos


# ---------------------------------------------------------------- names

def name_key(name: str) -> str:
    """Same key for 'Fred again..' and 'fred AGAIN', so they count as one artist."""
    return " ".join(word_list(name)) or name.casefold().strip()


def split_artists(value: str) -> list:
    """'A feat. B; C' -> ['A', 'B', 'C']. '&' and ',' stay put ('Simon & Garfunkel')."""
    out = []
    for part in FEAT_RE.split(clean_text(value)):
        part = part.strip()
        if part and len(part) <= 80 and name_key(part) not in NOT_AN_ARTIST:
            out.append(part)
    return out


def name_parts(name: str) -> list:
    """'A & B', 'A, B' or 'A x B' -> ['A', 'B'] ([] when it isn't a combination)."""
    parts = [p.strip() for p in COMBO_RE.split(name) if p.strip()]
    return parts if len(parts) > 1 else []


_BRACKETS_RE = re.compile(r"\s*[\(\[（【].*?[\)\]）】]")
_ALIAS_RE = re.compile(r"[\(\[（【]\s*(?:CV\s*[:.：．]\s*)?(.*?)\s*[\)\]）】]", re.IGNORECASE)


def alias_names(name: str) -> list:
    """Names inside brackets: the voice actor of 'Character(CV:Actor)', or the alias in 'Name【Alias】'."""
    return [a for a in (m.strip() for m in _ALIAS_RE.findall(TOPIC_RE.sub("", name))) if a]


def query_variants(name: str) -> list:
    """The name as tagged, then without (brackets) that only confuse the search, then what was inside them."""
    bare = _BRACKETS_RE.sub("", TOPIC_RE.sub("", name)).strip()
    out = [name] + ([bare] if bare and bare != name else [])
    return out + [a for a in alias_names(name) if a not in out]


def safe_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", unicodedata.normalize("NFC", clean_text(name)))
    return s.strip().lstrip(".")[:120].strip() or "artist"


def artist_homes(music_dir) -> dict:
    """name_key -> the artist's own folder at the top of the library, for placement = "artist_folder".
    A name that more than one folder spells alike is left out: there's no telling which one is meant."""
    found = defaultdict(list)
    try:
        for d in sorted(Path(music_dir).iterdir()):
            if d.is_dir() and not d.name.startswith((".", "$")):
                found[name_key(d.name)].append(d)
    except OSError:
        return {}
    return {key: dirs[0] for key, dirs in found.items() if key and len(dirs) == 1}


def _home(homes, name: str):
    return homes.get(name_key(name)) if homes else None


def existing_image(out_dir: Path, name: str, homes: dict = None):
    """The picture already saved for `name`: in out_dir as <Artist>.jpg or, with `homes`, as artist.jpg in its own folder."""
    stem = safe_filename(name)
    found = next((p for ext in IMAGE_EXTS if (p := out_dir / (stem + ext)).is_file()), None)
    home = _home(homes, name)
    return found or (next((p for ext in IMAGE_EXTS if (p := home / ("artist" + ext)).is_file()), None) if home else None)


def has_picture(out_dir: Path, name: str, homes: dict = None) -> bool:
    """True if `name` has a picture, or is 'A & B' and both A and B do."""
    if existing_image(out_dir, name, homes):
        return True
    parts = name_parts(name)
    return bool(parts) and all(existing_image(out_dir, part, homes) for part in parts)


# ---------------------------------------------------------------- reading the library

def tag_artists(path: Path) -> list:
    try:
        tags = getattr(MutagenFile(path), "tags", None)
        if not tags:
            return []
        if isinstance(tags, ID3):
            frame = tags.get("TPE1")
            values = frame.text if frame else []
        elif isinstance(tags, MP4Tags):
            values = tags.get("\xa9ART", [])
        else:
            values = tags.get("artist", [])
        return [str(v) for v in values]
    except Exception:
        return []


def artists_of(path: Path) -> list:
    """Artist names for one song: from its tag, else from an 'Artist - Title' filename."""
    names = [n for value in tag_artists(path) for n in split_artists(value)]
    if not names:
        parsed = parse_filename(clean_text(path.stem))
        if parsed["pattern"] in ("artist_dash_title", "artist_paren_details") and parsed["artist"]:
            names = split_artists(parsed["artist"])
    return names


def collect(files: list, workers: int, quiet: bool) -> list:
    """One Job per artist, most songs first."""
    with ThreadPoolExecutor(workers) as pool:
        per_song = list(progress(pool.map(artists_of, files), len(files), "Reading artists", quiet, unit="song"))
    spellings, songs = {}, Counter()
    for names in per_song:
        counted = set()
        for n in names:
            key = name_key(n)
            spellings.setdefault(key, Counter())[n] += 1
            if key not in counted:
                counted.add(key)
                songs[key] += 1
    return [Job(spellings[k].most_common(1)[0][0], n) for k, n in songs.most_common()]


# ---------------------------------------------------------------- Deezer

class RateLimiter:
    """Deezer allows 50 requests per 5 seconds; stay comfortably under that."""

    def __init__(self, per_second: float):
        self.gap, self.next, self.lock = 1 / per_second, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + self.gap
        if delay > 0:
            time.sleep(delay)


class GiveUp:
    """
    Stops asking a service that has started refusing us. YouTube answers a burst
    of searches with HTTP 403, and every refusal costs seconds of yt-dlp retries,
    so once it has refused a few times in a row there is nothing to gain by asking
    again this run.
    """

    def __init__(self, after: int):
        self.after, self.failures, self.lock = after, 0, threading.Lock()

    def given_up(self) -> bool:
        with self.lock:
            return self.failures >= self.after

    def record(self, worked: bool) -> None:
        with self.lock:
            self.failures = 0 if worked else self.failures + 1


API_LIMIT = RateLimiter(8)
YOUTUBE_LIMIT = RateLimiter(YOUTUBE_SEARCHES_PER_SECOND)
YOUTUBE_GAVE_UP = GiveUp(YOUTUBE_GIVE_UP_AFTER)


def deezer_get(path: str, **params) -> dict:
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        API_LIMIT.wait()
        try:
            data = json.loads(fetch_url(url, headers={"User-Agent": USER_AGENT}, timeout=20))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Deezer answered HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            if attempt < 3 and isinstance(reason, (ssl.SSLError, ConnectionError, TimeoutError)):
                time.sleep(1 + attempt)   # a dropped connection, not "offline": ask again on a fresh one
                continue
            raise RuntimeError(f"could not reach Deezer (offline?): {reason}") from e
        error = data.get("error") if isinstance(data, dict) else None
        if not error:
            return data
        if error.get("code") == 4:  # quota exceeded: wait a bit and try again
            time.sleep(2 + 2 * attempt)
            continue
        raise RuntimeError(f"Deezer error: {error.get('message', error)}")
    raise RuntimeError("Deezer is rate-limiting requests; try again in a minute or use --workers 2")


def real_picture(url: str) -> bool:
    return bool(url) and not any(marker in url for marker in NO_PICTURE_MARKERS)


def picture_of(artist: dict) -> str:
    """The biggest real picture Deezer has for this artist, or ''."""
    for key in ("picture_xl", "picture_big", "picture_medium"):
        if real_picture(artist.get(key, "")):
            return artist[key]
    return ""


def search_artists(name: str) -> list:
    """
    Deezer artists that might be `name`, without duplicates. The fallback queries
    are only tried when the ones before them found nobody with exactly this name,
    so the common case costs a single request.
    """
    seen, found = set(), []
    for query in query_variants(name):
        for artist in deezer_get("search/artist", q=query, limit=10).get("data") or []:
            if isinstance(artist, dict) and artist.get("id") not in seen:
                seen.add(artist.get("id"))
                found.append(artist)
        if exact_match(found, name)[0]:
            break
    return found


def make_pick(name: str, artist: dict) -> Pick:
    return Pick(name, artist.get("name", ""), int(artist.get("nb_fan") or 0),
                artist.get("link", ""), picture_of(artist))


def exact_match(found: list, name: str):
    """(artist or None, reason). The most popular artist whose name is exactly `name`."""
    keys = {name_key(n) for n in [name] + alias_names(name)}
    same = [a for a in found if name_key(a.get("name", "")) in keys]
    with_picture = [a for a in same if picture_of(a)]
    if with_picture:
        return max(with_picture, key=lambda a: a.get("nb_fan") or 0), ""
    if same:
        return None, "Deezer knows this artist but has no picture of them"
    return None, ""


# ---------------------------------------------------------------- YouTube (fallback for artists Deezer doesn't have)

def big_avatar(thumbnails: list) -> str:
    """The biggest channel picture in a yt-dlp thumbnail list, bumped up to 800px (YouTube serves any size asked for)."""
    if not thumbnails:
        return ""
    url = max(thumbnails, key=lambda t: t.get("width") or 0).get("url", "")
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    return YOUTUBE_AVATAR_SIZE_RE.sub("=s800-", url)


def _youtube_entries(url: str) -> list:
    """The first page of results of a YouTube search URL ([] when yt-dlp is missing, refused or gave up)."""
    if not shutil.which("yt-dlp") or YOUTUBE_GAVE_UP.given_up() or STOP.is_set():
        return []
    YOUTUBE_LIMIT.wait()
    try:
        r = subprocess.run(["yt-dlp", "--no-warnings", "--flat-playlist",
                            "--playlist-end", str(YOUTUBE_RESULTS),
                            "--extractor-retries", str(YOUTUBE_RETRIES), "-J", url],
                           capture_output=True, text=True, timeout=YOUTUBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        YOUTUBE_GAVE_UP.record(False)
        return []
    YOUTUBE_GAVE_UP.record(r.returncode == 0)
    if r.returncode != 0:
        return []
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    return [e for e in entries if isinstance(e, dict)]


def youtube_channels(name: str) -> list:
    """
    Channels on YouTube that might be `name`, shaped like Deezer's artist dicts
    (name/nb_fan/link/picture_xl) so the rest of the pipeline can treat a
    YouTube channel exactly like a Deezer artist. Used as a fallback: Deezer
    doesn't have every artist, especially smaller or YouTube-only ones.
    """
    out = []
    for e in _youtube_entries(YOUTUBE_CHANNEL_SEARCH.format(urllib.parse.quote_plus(name))):
        # Only channels: a video's thumbnail is a frame of the video, not a picture of the artist
        if e.get("ie_key") != "YoutubeTab" or not e.get("channel"):
            continue
        picture = big_avatar(e.get("thumbnails") or [])
        if not picture:
            continue
        out.append({"name": TOPIC_CHANNEL_RE.sub("", e["channel"]).strip(),
                    "nb_fan": int(e.get("channel_follower_count") or 0),
                    "link": e.get("channel_url") or e.get("url") or "",
                    "picture_xl": picture})
    return out


def popular_video_channels(name: str, limit: int = 3) -> list:
    """
    Channels that uploaded the most-watched videos for `name`, as channel dicts with a
    picture. For artists whose own channel is named differently (a romanised name, a
    label, a "- Topic" channel): the videos still say who they are. Only videos with
    the name in their title count: YouTube pads a search for an obscure name with
    popular but unrelated videos, and those channels are noise. Channels named after
    the artist come first, then the most-viewed.
    """
    wanted = "".join(word_list(name))
    if not wanted:
        return []
    views, info = {}, {}
    for e in _youtube_entries(YOUTUBE_VIDEO_SEARCH.format(urllib.parse.quote_plus(name))):
        cid, channel = e.get("channel_id"), e.get("channel")
        if e.get("ie_key") != "Youtube" or not cid or not channel:
            continue
        if wanted not in "".join(word_list(e.get("title") or "")) + "".join(word_list(channel)):
            continue
        views[cid] = views.get(cid, 0) + int(e.get("view_count") or 0)
        info[cid] = channel
    named_after_it = lambda cid: wanted in "".join(word_list(info[cid]))
    out = []
    for cid in sorted(views, key=lambda c: (not named_after_it(c), -views[c]))[:limit]:
        # A video entry has no channel picture, so look the channel up by name and keep the one with this id
        mine = [c for c in youtube_channels(info[cid]) if cid in c["link"]]
        if mine:
            out.append(mine[0])
    return out


# ---------------------------------------------------------------- images

def download(url: str) -> bytes:
    data = fetch_url(url, headers={"User-Agent": USER_AGENT}, timeout=30, max_bytes=MAX_IMAGE_BYTES)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is bigger than 20 MB")
    return data


def as_jpeg(data: bytes) -> bytes:
    """Check that `data` is a usable picture and return it as JPEG (JPEGs are kept as they are)."""
    img = Image.open(io.BytesIO(data))
    img.load()
    if min(img.size) < 100:
        raise ValueError(f"image is too small ({img.width}x{img.height})")
    if img.format == "JPEG":
        return data
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


def fetch_picture(pick: Pick) -> None:
    """Download and check the picture; leaves pick.image empty (and says why) on failure."""
    try:
        pick.image = as_jpeg(download(pick.picture_url))
    except Exception as e:
        pick.image = b""
        raise RuntimeError(f"couldn't download the picture for {pick.name}: {e}") from e


def pick_from_text(name: str, text: str) -> Pick:
    """A Deezer artist link, an image link, or an image file the user gave us."""
    try:
        parts = shlex.split(text)  # a file dragged into the terminal arrives quoted/escaped
        text = parts[0] if len(parts) == 1 else text
    except ValueError:
        pass
    text = text.strip().strip("'\"")
    m = DEEZER_ARTIST_RE.search(text)
    if m:
        artist = deezer_get(f"artist/{m.group(1)}")
        if not picture_of(artist):
            raise ValueError("Deezer has no picture for that artist")
        pick = make_pick(name, artist)
    elif text.lower().startswith(("http://", "https://")):
        pick = Pick(name, picture_url=text)
    elif Path(text).expanduser().is_file():
        pick = Pick(name)
        pick.image = as_jpeg(Path(text).expanduser().read_bytes())
        return pick
    else:
        raise ValueError("that's not a Deezer artist link, an image link, or an image file")
    fetch_picture(pick)
    return pick


# ---------------------------------------------------------------- finding pictures

def search_job(job: Job, opts: dict) -> Job:
    """
    Search Deezer for one artist. Never prompts or raises. Leaves the job "found"
    (its pictures still to be downloaded), "unsure" (needs a choice) or "failed".
    """
    if STOP.is_set():
        job.status, job.reason = "failed", "stopped before searching"
        return job
    try:
        return _search_job(job, opts)
    except Exception as e:
        job.status, job.reason = "failed", str(e) or type(e).__name__
        return job


def _find(name: str) -> tuple:
    """(exact match or None, reason, everyone found). Tries Deezer first, then YouTube channels."""
    found = search_artists(name)
    artist, reason = exact_match(found, name)
    if not artist:
        yt, seen = [], set()
        for query in query_variants(name):
            for channel in youtube_channels(query):
                if channel["link"] not in seen:
                    seen.add(channel["link"])
                    yt.append(channel)
            if exact_match(yt, name)[0]:
                break
        yt_artist, yt_reason = exact_match(yt, name)
        found, artist, reason = found + yt, yt_artist, reason or yt_reason
    return artist, reason, found


def _search_job(job: Job, opts: dict) -> Job:
    artist, reason, found = _find(job.name)
    if artist:
        job.picks = [make_pick(job.name, artist)]
    else:
        # "A & B" isn't an artist of its own: look for A and B separately, at the
        # same time, so a three-way collaboration costs one lookup's wait, not three
        parts = [p for p in name_parts(job.name) if name_key(p) not in GENRE_WORDS]
        if parts and not STOP.is_set():
            with ThreadPoolExecutor(min(len(parts), 4)) as pool:
                hits = list(pool.map(lambda part: (part, _find(part)[0]), parts))
            job.picks = [make_pick(part, hit) for part, hit in hits if hit]

    if job.picks:
        job.status = "found"
        return job
    if not reason:
        close = sorted((a for a in found if match_score(job.name, a.get("name", "")) >= 0.5),
                       key=lambda a: (-match_score(job.name, a["name"]), -(a.get("nb_fan") or 0)))
        job.candidates = [a for a in close if picture_of(a)][: opts["search_results"]]
    if not job.candidates:
        # last resort: whoever uploaded the most-watched videos for this name
        for query in query_variants(job.name):
            job.candidates = popular_video_channels(query)[: opts["search_results"]]
            if job.candidates:
                job.by_views = True
                break
    if job.candidates:
        job.status = "unsure"
    elif reason:
        job.status, job.reason = "failed", reason
    else:
        job.status, job.reason = "failed", ("no artist with that name on Deezer (YouTube is rate-limiting us)"
                                            if YOUTUBE_GAVE_UP.given_up() else
                                            "no artist with that name on Deezer or YouTube")
    return job


def download_pick(pick: Pick) -> str:
    """Download one picture. Returns "" or what went wrong; never raises."""
    if STOP.is_set():
        return f"stopped before downloading the picture for {pick.name}"
    try:
        fetch_picture(pick)
        return ""
    except Exception as e:
        return str(e) or type(e).__name__


def finish_job(job: Job, errors: list) -> Job:
    """Keep the pictures that downloaded; the job is ready if any did."""
    job.picks = [p for p in job.picks if p.image]
    job.status = "ready" if job.picks else "failed"
    job.reason = "; ".join(errors)
    return job


def resolve_job(job: Job, opts: dict) -> Job:
    """Search and download for one artist in the calling thread."""
    search_job(job, opts)
    if job.status == "found":
        finish_job(job, [e for e in (download_pick(p) for p in job.picks) if e])
    return job


def find_pictures(jobs: list, opts: dict, workers: int, quiet: bool) -> None:
    """
    Look everyone up with two thread pools working at the same time: one searches
    Deezer (which allows about 8 requests a second), the other downloads the
    pictures. A slow download never holds up the next search, and one artist's
    pictures ("A & B" -> A and B) download side by side.
    """
    finished, lock, waiting, errors = queue.Queue(), threading.Lock(), {}, {}
    with ThreadPoolExecutor(workers) as searches, ThreadPoolExecutor(workers) as downloads:
        def download(job, pick):
            try:
                error = download_pick(pick)
                with lock:
                    if error:
                        errors[id(job)].append(error)
                    waiting[id(job)] -= 1
                    last = waiting[id(job)] == 0
                if last:
                    finish_job(job, errors.pop(id(job)))
            except Exception as e:  # never leave the progress bar waiting for this artist
                job.status, job.reason = "failed", str(e) or type(e).__name__
                last = True
            if last:
                finished.put(job)

        def search(job):
            try:
                search_job(job, opts)
                if job.status == "found":
                    with lock:
                        waiting[id(job)], errors[id(job)] = len(job.picks), []
                    for pick in list(job.picks):
                        downloads.submit(download, job, pick)
                    return
            except Exception as e:
                job.status, job.reason = "failed", str(e) or type(e).__name__
            finished.put(job)

        for job in jobs:
            searches.submit(search, job)
        for _ in progress(range(len(jobs)), len(jobs), "Finding artists", quiet, unit="artist"):
            finished.get()


def review(job: Job) -> None:
    """Ask the user to choose for an artist without an exact match."""
    print(f"\n  {yellow('?')} {bold(job.name)}  {dim('· ' + plural(job.songs, 'song'))}")
    print(dim("    channels of the most-watched YouTube videos for this name:" if job.by_views
              else "    no artist on Deezer has exactly that name. Closest:"))
    width = max(text_width(a["name"]) for a in job.candidates)
    for i, a in enumerate(job.candidates, 1):
        print(f'      {cyan(str(i))}  {fit(a["name"], min(width, 32))}  '
              f'{dim(fit(compact(int(a.get("nb_fan") or 0)) + " fans", 12))} {dim(a.get("link", ""))}')
    while True:
        ans = input(f"    {cyan('❯')} pick 1-{len(job.candidates)}, paste a Deezer artist link / image link / image file, "
                    f"or {dim('Enter to skip')}: ").strip()
        if not ans:
            job.status, job.reason = "failed", "skipped by you"
            return
        try:
            if ans.isdigit() and 1 <= int(ans) <= len(job.candidates):
                pick = make_pick(job.name, job.candidates[int(ans) - 1])
                fetch_picture(pick)
            else:
                pick = pick_from_text(job.name, ans)
        except Exception as e:
            print(red(f"    {e}"))
            continue
        job.picks, job.status = [pick], "ready"
        return


def save_picture(out_dir: Path, pick: Pick, force: bool, homes: dict = None) -> None:
    if STOP.is_set():
        pick.saved = "error: stopped before saving"
        return
    if not force and existing_image(out_dir, pick.name, homes):
        pick.saved = "exists"
        return
    home = _home(homes, pick.name)
    target = home / "artist.jpg" if home else out_dir / (safe_filename(pick.name) + ".jpg")
    try:
        atomic_write(target, pick.image)
        pick.saved = "saved"
    except OSError as e:
        pick.saved = f"error: {e.strerror or e}"


# ---------------------------------------------------------------- main

def compact(n: int) -> str:
    """1234567 -> '1.2M', 8500 -> '8.5K'."""
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def fact(label: str, value) -> None:
    log(f"  {dim(fit(label, 11))}{value}")


def report(todo: list, have: list, out_dir: Path, dry_run: bool, homes: dict = None) -> None:
    ready = [j for j in todo if j.status == "done" or (dry_run and j.status == "ready")]
    failed = [j for j in todo if j.status == "failed"]
    picks = [(j, p) for j in ready for p in j.picks]
    width = min(30, max([text_width(n) for n in [p.name for _, p in picks] + [j.name for j in failed]] or [8]))

    def songs(j):
        return dim(fit(plural(j.songs, "song") if j.songs else "", 9))

    if picks:
        section("Pictures found" if dry_run or not any(p.saved == "saved" for _, p in picks) else "Pictures saved")
        for j, p in picks:
            bad = p.saved.startswith("error")
            mark = red("✗") if bad else dim("•") if p.saved == "exists" else green("✓")
            if bad:
                detail = red(p.saved)
            elif not p.deezer_name:
                detail = dim("your image")
            else:
                detail = f"{fit(compact(p.fans) + ' fans' if p.fans else '', 12)}"
                detail += dim(f'as "{p.deezer_name}"  ' if name_key(p.deezer_name) != name_key(p.name) else "")
                detail += dim(p.link.replace("https://www.", ""))
                if p.saved == "exists":
                    detail = dim("already had a picture")
            print(f"  {mark} {fit(p.name, width)}  {songs(j)}  {detail}")

    if failed:
        section("Couldn't find")
        for j in failed:
            print(f"  {red('✗')} {fit(j.name, width)}  {songs(j)}  {dim(j.reason or 'no match')}")
            print(dim(f"      ↳ {PROG} artists --artist {shlex.quote(j.name)} --image LINK_OR_FILE"))
        print(dim("\n  Find the artist on deezer.com, then run a ↳ line with their artist link,"
                  "\n  or with any image link or image file."))

    saved = sum(p.saved == "saved" for _, p in picks)
    print("\n  " + dim("─" * 46))
    print("  " + "   ".join([
        green(f"✓ {saved if not dry_run else len(picks)} {'would save' if dry_run else 'saved'}"),
        dim(f"• {len(have)} already had one"),
        (red if failed else dim)(f"✗ {len(failed)} not found"),
    ]))
    if saved:
        print(dim(f"  Pictures are in each artist's own folder (artist.jpg), or in {out_dir}" if homes else f"  Pictures are in {out_dir}"))
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/folders to read artists from (default: artist_art.folders from the config)")
    ap.add_argument("--config", help="path to a config.toml")
    ap.add_argument("--artist", action="append", metavar="NAME", help="look up this artist instead of scanning songs (repeat for several)")
    ap.add_argument("--image", metavar="LINK_OR_FILE", help="use this Deezer artist link, image link or image file for the one --artist (implies --force)")
    ap.add_argument("--list-missing", action="store_true", help="only list artists without a picture; no downloading")
    ap.add_argument("--dry-run", action="store_true", help="look artists up but don't save anything")
    ap.add_argument("--force", action="store_true", default=None, help="replace pictures that already exist")
    ap.add_argument("--auto", action="store_true", help="never ask; skip artists without an exact match")
    ap.add_argument("--workers", type=int, choices=range(1, 65), metavar="1-64", help="parallel lookups")
    ap.add_argument("--placement", choices=("shared", "artist_folder"),
                    help="shared: all pictures in one folder; artist_folder: artist.jpg inside each artist's own folder")
    ap.add_argument("--no-progress", action="store_true", help="hide progress bars")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start_log("artist-art", cfg)
    opts = dict(cfg["artist_art"])
    if args.placement:
        opts["placement"] = args.placement
    homes = artist_homes(cfg["music_dir"]) if opts["placement"] == "artist_folder" else None
    if args.force:
        opts["force"] = True
    workers = max(1, min(64, args.workers or cfg["workers"]))
    quiet = args.no_progress
    heading("Artist pictures", "from Deezer" if not args.list_missing else "artists without a picture")

    out_dir = resolve(opts["output_dir"], cfg["music_dir"])
    if not Path(opts["output_dir"]).expanduser().is_absolute():
        problem = folder_problem(cfg["music_dir"])
        if problem:
            log(red(f"  ✗ music folder not reachable: {cfg['music_dir']}"))
            log(f"  {problem}")
            if not args.list_missing:
                sys.exit("Not saving pictures into a folder that can't be reached.")

    if args.image and (not args.artist or len(args.artist) != 1):
        sys.exit("--image needs exactly one --artist NAME.")
    if args.image:
        opts["force"] = True

    # 1. which artists
    if args.artist:
        names = {}
        for n in (n for a in args.artist for n in split_artists(a)):
            names.setdefault(name_key(n), n)
        jobs = [Job(n) for n in names.values()]
        if not jobs:
            sys.exit("--artist needs a name.")
    else:
        paths = args.paths or [resolve(f, cfg["music_dir"]) for f in opts["folders"]]
        files = list(find_audio(paths, AUDIO_EXTS | UNSUPPORTED_EXTS))
        fact("Songs", f"{len(files)} in {plural(len(paths), 'folder')}")
        jobs = collect(files, workers, quiet)

    # 2. which of them still need a picture
    have, todo = [], []
    for j in jobs:  # one pass; `j not in have` would compare whole dataclasses field by field, O(n^2)
        (have if not opts["force"] and has_picture(out_dir, j.name, homes) else todo).append(j)
    fact("Artists", f"{len(jobs)}  {dim('·')}  {len(have)} already have a picture  {dim('·')}  {bold(str(len(todo)))} to find")
    fact("Saving to", f"each artist's own folder (artist.jpg), else {out_dir}" if homes else str(out_dir))
    if args.list_missing:
        width = min(32, max([text_width(j.name) for j in todo] or [8]))
        section("Without a picture" if todo else "Every artist has a picture")
        for j in todo:
            print(f"  {yellow('•')} {fit(j.name, width)}  {dim(plural(j.songs, 'song') if j.songs else '')}")
        print()
        return
    if not todo:
        print(f"\n  {green('✓')} Every artist already has a picture.\n")
        return

    # 3. look them up in parallel
    if args.image:
        job = todo[0]
        try:
            job.picks, job.status = [pick_from_text(job.name, args.image)], "ready"
        except Exception as e:
            sys.exit(f"Could not use that image: {e}")
    else:
        find_pictures(todo, opts, workers, quiet)

    # 4. ask about artists without an exact match (after the bars, so prompts aren't interleaved)
    unsure = [j for j in todo if j.status == "unsure"]
    if unsure:
        if args.auto or not sys.stdin.isatty():
            for j in unsure:
                j.status, j.reason = "failed", "no exact match (use without --auto to choose)"
        elif not STOP.is_set():
            print(f"\n  {pink('♪')} {plural(len(unsure), 'artist')} {'needs' if len(unsure) == 1 else 'need'} you to pick a match {dim('(Ctrl-C skips the rest)')}")
            try:
                with normal_ctrl_c():
                    for j in unsure:
                        review(j)
            except (KeyboardInterrupt, EOFError):
                print("\n  Skipping the rest.")
        for j in unsure:
            if j.status == "unsure":
                j.status, j.reason = "failed", "skipped"

    # 5. save
    ready = [j for j in todo if j.status == "ready"]
    if ready and not args.dry_run:
        if not homes or any(name_key(p.name) not in homes for j in ready for p in j.picks):
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                sys.exit(f"Could not create {out_dir}: {e.strerror or e}")
        picks, seen = [], set()
        for j in ready:
            for p in j.picks:
                if safe_filename(p.name).casefold() not in seen:  # "A" and "A & B" both find A
                    seen.add(safe_filename(p.name).casefold())
                    picks.append(p)
        with ThreadPoolExecutor(min(workers, 4)) as pool:  # disk-bound; don't thrash the drive
            list(progress(pool.map(lambda p: save_picture(out_dir, p, opts["force"], homes), picks),
                          len(picks), "Saving pictures", quiet, unit="picture"))
        for j in ready:
            j.status = "done"
            for p in j.picks:
                p.saved = p.saved or "exists"  # found again under another name this run

    # 6. report
    report(todo, have, out_dir, args.dry_run, homes)


if __name__ == "__main__":
    install_stop_handler()
    sys.exit(main())
