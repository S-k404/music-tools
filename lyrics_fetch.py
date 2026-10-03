"""
Original lyrics for a song, from lrclib.net (a free, community-sourced lyrics
database; no account or key needed). Prefers time-synced lyrics (LRC) so the
lyrics tool can highlight the current line and export a real .lrc file; falls
back to plain, unsynced lyrics when that's all lrclib has.

Searches are matched on title, artist and length, so a song lrclib doesn't have
is reported as not found instead of getting some other song's lyrics.
"""

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlencode

from lyrics_lang import fold_layers, guess_language, norm
from lyrics_render import Line
from lyrics_romanize import SUPPORTED
from lyrics_translate import NetError, request_json

API = "https://lrclib.net/api"
USER_AGENT = "music-tools (lyrics)"
MAX_EXACT_DURATION = 3600   # lrclib's "get" endpoint only accepts a duration of 1-3600 seconds


class LyricsError(Exception):
    """The lookup failed: lrclib.net couldn't be reached, or answered something unusable."""


class NotFound(LyricsError):
    """lrclib.net was reached and definitely has nothing that matches this song."""


@dataclass
class Lyrics:
    lines: list          # Line objects, original text only (romaji/english filled in later)
    synced: bool          # True when lines have real timestamps
    artist: str = ""       # as lrclib has it, which may differ slightly from the tag
    title: str = ""
    source: str = "lrclib.net"


_cache: dict = {}            # (endpoint, sorted params) -> (time answered, parsed JSON)
_cache_lock = threading.Lock()
CACHE_SECONDS, CACHE_MAX = 600, 4000


def api_get(path: str, **params) -> object:
    """GET one lrclib endpoint and return the parsed JSON, or None on a 404. Busy servers are retried.
    Answers are remembered for a few minutes: a song's spelling variants often boil down to the same
    query, and there's no point asking lrclib the same thing twice in one run."""
    params = {k: v for k, v in params.items() if v not in (None, "")}
    key = (path, tuple(sorted(params.items())))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        data = request_json(f"{API}/{path}?{urlencode(params)}", "lrclib.net", headers={"User-Agent": USER_AGENT})
    except NetError as e:
        raise LyricsError(str(e)) from e
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()  # simple and bounded; a refill only costs a few repeated requests
        _cache[key] = (now, data)
    return data


LRC_TAG_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str) -> list:
    """Synced LRC text -> Line objects. A line with several stamps repeats once per stamp."""
    lines = []
    for raw in text.splitlines():
        stamps = LRC_TAG_RE.findall(raw)
        if not stamps:
            continue
        content = LRC_TAG_RE.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append(Line(int(minutes) * 60 + float(seconds), content))
    lines.sort(key=lambda l: l.time)
    return lines


def parse_plain(text: str) -> list:
    """Lyrics with no timestamps -> Line objects; blank lines become verse breaks."""
    rows = [r.strip() for r in text.splitlines()]
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    out, previous_blank = [], True
    for row in rows:
        if not row and previous_blank:
            continue
        out.append(Line(None, row))
        previous_blank = not row
    return out


CREDIT_RE = re.compile(
    r"^\s*(?:作词|作詞|作曲|编曲|編曲|词曲|詞曲|制作人|制作|監修|监制|混音|录音|母带|词|詞|曲|작사|작곡|편곡|lyrics?(?:\s+by)?|music(?:\s+by)?|composer|"
    r"composed\s+by|arranger|arranged\s+by|written\s+by|words(?:\s+by)?|lyricist|producer|produced\s+by)\s*[:：]",
    re.IGNORECASE)


def strip_credits(lines: list, artist: str, title: str) -> list:
    """Drop the credit lines ('作词 : X') and 'Title - Artist' header that many lrclib entries start with."""
    headers = {norm(f"{a} {t}") for a in (artist,) for t in (title,)} | {norm(f"{title} {artist}")}
    out, checked = list(lines), 0
    while out and checked < 8:
        first = out[0]
        if not first.text or CREDIT_RE.match(first.text) or norm(first.text) in headers:
            out.pop(0)
            checked += 1
        else:
            break
    return out or lines


def tidy_title(text: str) -> str:
    """A title without (feat. X), [Official Video] and similar, for comparing and searching."""
    text = unicodedata.normalize("NFC", text)
    stripped = " ".join(re.sub(r"[\(\[（【][^\)\]）】]*[\)\]）】]", " ", text).split()).strip(" -_")
    if stripped:
        return stripped
    # the whole title was one bracketed group ("(Original / Romanized title)"): that
    # bracket wasn't decoration, it WAS the title, so keep what's inside instead of
    # discarding the entire thing
    inner = re.sub(r"^\s*[\(\[（【]\s*|\s*[\)\]）】]\s*$", "", text)
    return " ".join(inner.split()).strip(" -_") or " ".join(text.split()).strip(" -_")


def first_artist(artist: str) -> str:
    """'A feat. B', 'A & B' or 'A; B' -> 'A'."""
    return re.split(r"\s*(?:;|,|&|/|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*", artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def _artist_score(artist: str, found: str) -> float:
    from fix_album_art import match_score  # imported lazily: avoids a load-time cycle
    a, b = norm(artist), norm(found)
    if a and b and (a in b or b in a):
        return 1.0
    return match_score(artist, found)


def _usable(hit) -> bool:
    return isinstance(hit, dict) and not hit.get("instrumental") and bool(hit.get("syncedLyrics") or hit.get("plainLyrics"))


def _duration_gap(hit: dict, duration):
    try:
        return abs(float(hit["duration"]) - float(duration)) if duration and hit.get("duration") else None
    except (TypeError, ValueError, KeyError):
        return None


def best_hit(hits: list, artist: str, title: str, duration: float = None) -> dict:
    """
    The record that is really this song, or None. A record must have the same
    title, and either the same artist or (for artists lrclib spells another way,
    like kanji) the same length. Synced records of the same recording win.
    Instrumentals and records of a different length (another recording) are ignored.
    """
    from fix_album_art import match_score
    want, best, best_score = tidy_title(title), None, 0.0
    for hit in hits:
        if not _usable(hit):
            continue
        exact = bool(hit.get("_exact"))  # lrclib's own exact match on artist + title (+ length)
        t_score = match_score(want, tidy_title(str(hit.get("trackName") or "")))
        gap = _duration_gap(hit, duration)
        a_score = _artist_score(artist, str(hit.get("artistName") or "")) if artist else None
        if not exact:
            if t_score < 0.6 or (gap is not None and gap > 12):
                continue
            if not ((a_score or 0) >= 0.3 or (gap is not None and gap <= 2)):
                continue
        synced = bool(hit.get("syncedLyrics")) and (gap is None or gap <= 4)
        score = 2 * t_score + (a_score or 0) + (2.0 if synced else 0) + (0.25 if exact else 0) - (gap or 0) / 10
        if score > best_score:
            best, best_score = hit, score
    return best


def _choose_text(hit: dict, duration) -> tuple:
    """(lyrics text, synced?). Timestamps are only trusted for a recording of the same length."""
    gap = _duration_gap(hit, duration)
    synced_text = hit.get("syncedLyrics") or ""
    if synced_text and (gap is None or gap <= 4 or not hit.get("plainLyrics")):
        return synced_text, True
    return hit.get("plainLyrics") or "", False


def fetch(artist: str, title: str, album: str = "", duration: float = None) -> Lyrics:
    """
    Original lyrics for one song. Tries lrclib's exact match first (fast, and its
    best chance of picking the right version), then searches, and keeps looking
    for a time-synced copy of the same recording if all it found was plain text.
    Raises NotFound if nothing matches, LyricsError if lrclib can't be reached.
    """
    artist, title = artist.strip(), title.strip()
    attempts = [(artist, title)]
    tidy_pair = (first_artist(artist), tidy_title(title))
    if tidy_pair[1] and tidy_pair != attempts[0]:
        attempts.append(tidy_pair)

    seen, best = [], None

    def consider(records):
        nonlocal best
        seen.extend(r for r in (records if isinstance(records, list) else []) if isinstance(r, dict))
        best = best_hit(seen, artist, title, duration)
        return best is not None and _choose_text(best, duration)[1]

    for a, t in attempts:
        if a:
            # lrclib's exact-match endpoint rejects a duration outside 1-3600s with a hard
            # "ValidationError" (HTTP 400) instead of just not matching; a file that long is a
            # mix/compilation, not a song, so leave duration out rather than send one guaranteed
            # to be refused. Either way, a failure here is just "no exact match" - search still runs.
            get_duration = round(duration) if duration and duration <= MAX_EXACT_DURATION else None
            try:
                exact = api_get("get", artist_name=a, track_name=t, album_name=album, duration=get_duration)
            except LyricsError:
                exact = None
            if _usable(exact):
                if consider([{**exact, "_exact": True}]):
                    break
        if consider(api_get("search", artist_name=a, track_name=t)):
            break
    title_only_done = False
    if best is None or not _choose_text(best, duration)[1]:
        # lrclib may spell the artist another way (kanji, a different transliteration):
        # look by title alone, which is only safe because the length has to match too
        if duration:
            consider(api_get("search", track_name=attempts[-1][1]))
            title_only_done = True

    # A Latin-script record that isn't English may be somebody's romanization of a song whose real lyrics
    # (kanji, hangul) are also on lrclib. Translation can't read romaji, and you want the original kept: prefer those.
    if best and duration and guess_language(_choose_text(best, duration)[0]) == "und":
        if not title_only_done:
            consider_more = api_get("search", track_name=attempts[-1][1])
            seen.extend(r for r in (consider_more if isinstance(consider_more, list) else []) if isinstance(r, dict))
        native = [h for h in seen if _usable(h)
                  and guess_language(h.get("plainLyrics") or h.get("syncedLyrics") or "") in SUPPORTED]
        original = best_hit(native, artist, title, duration)
        if original:
            best = original

    if not best:
        raise NotFound("no lyrics found on lrclib.net" + ("" if artist else " (the song has no artist to match on)"))
    text, synced = _choose_text(best, duration)
    found_artist, found_title = best.get("artistName") or artist, best.get("trackName") or title
    lines = parse_lrc(text) if synced else parse_plain(text)
    lines = strip_credits(fold_layers(lines) if synced else lines, found_artist, found_title)
    if not lines:
        raise NotFound("lrclib.net has an entry for this song but no lyrics text")
    return Lyrics(lines, synced, found_artist, found_title)
