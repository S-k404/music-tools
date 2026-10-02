"""
Lyrics you already have: a .lrc file next to the song, or lyrics stored in the
song's own tags. Used when lrclib.net has nothing, and preferred over it when
your own .lrc is time-synced. Also reads a file you point `--lyrics` at.

Real-world .lrc files are messy (offsets, karaoke word tags, several timestamps
on a line, metadata lines, odd encodings), so this parser is forgiving.
"""

import re
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from lyrics_fetch import Lyrics
from lyrics_lang import fold_layers, script_of
from lyrics_render import Line, timestamp

MAX_BYTES = 1_000_000
MAX_LINES = 1500
TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
META_TAG = re.compile(r"^\s*\[[A-Za-z#]+:[^\]]*\]\s*$")
OFFSET_TAG = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


class LocalLyricsError(Exception):
    """A lyrics file that can't be used, with a reason a person can act on."""


def parse_lyrics(text: str) -> tuple:
    """Lyrics text, .lrc or plain -> (lines, synced)."""
    stamped, everything, offset = [], [], 0.0
    for raw in text.replace("﻿", "").splitlines():
        m = OFFSET_TAG.match(raw)
        if m:
            offset = int(m.group(1)) / 1000  # positive means the lyrics should appear sooner
            continue
        if META_TAG.match(raw):
            continue
        rest, stamps = raw.strip(), []
        while (m := TIME_TAG.match(rest)):
            frac = m.group(3)
            stamps.append(int(m.group(1)) * 60 + int(m.group(2)) + (int(frac) / 10 ** len(frac) if frac else 0))
            rest = rest[m.end():].lstrip()
        body = " ".join(WORD_TAG.sub("", rest).split())
        stamped += [Line(t, body) for t in stamps]
        everything.append(Line(None, body))
    spoken = sum(1 for l in stamped if l.text)
    if len(stamped) >= 2 and spoken >= 2 and len(stamped) >= 0.5 * len(everything):
        stamped.sort(key=lambda l: l.time)  # stable: lines sharing a time keep their order
        for l in stamped:
            l.time = max(0.0, l.time - offset)
        return stamped, True
    lines, blank = [], True
    for l in everything:  # collapse runs of blank lines; drop leading and trailing ones
        if l.text or not blank:
            lines.append(l)
        blank = not l.text
    while lines and not lines[-1].text:
        lines.pop()
    return lines, False


def _plausible(text: str) -> float:
    """How much of a decoded text looks like real writing (ASCII, kana, hangul, kanji, ordinary punctuation).
    Text decoded with the wrong legacy encoding fills up with half-width katakana and oddities."""
    good = sum(1 for c in text if c.isascii() or c in "、。「」・…—〜～！？（）　"
               or (script_of(c) and not 0xFF61 <= ord(c) <= 0xFF9F))
    return good / max(1, len(text))


def decode_text(raw: bytes) -> str:
    """UTF-8 (or UTF-16 with a byte-order mark) if it is, else the legacy encoding that fits best: older
    Japanese, Korean and Chinese .lrc files are often Shift-JIS, EUC-KR or GBK."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    best = None
    for encoding in ("cp932", "euc-kr", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if best is None or _plausible(text) > best[0]:
            best = (_plausible(text), text)
    return best[1] if best else raw.decode("utf-8", errors="replace")


def read_text_file(path: Path) -> str:
    if path.stat().st_size > MAX_BYTES:
        raise LocalLyricsError(f"{path.name} is too big to be lyrics")
    return decode_text(path.read_bytes())


def made_by_us(path: Path) -> bool:
    """Whether music-tools wrote this file (so it's safe to write again, and isn't a source of lyrics)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"[by:music-tools" in head or b'content="music-tools"' in head


def embedded_lyrics(song: Path) -> str:
    """Lyrics stored in the song's tags ('' if there are none, or the tags can't be read)."""
    try:
        ext = song.suffix.lower()
        if ext == ".mp3":
            tags = ID3(song)
            for fr in tags.getall("SYLT"):
                if getattr(fr, "format", 0) == 2 and fr.text:  # timestamps in milliseconds
                    return "\n".join(f"[{timestamp(ms / 1000)}]{' '.join(str(t).split())}" for t, ms in fr.text)
            for fr in tags.getall("USLT"):
                if str(fr.text).strip():
                    return str(fr.text)
        elif ext in (".m4a", ".mp4"):
            values = (MP4(song).tags or {}).get("\xa9lyr", [])
            if values and str(values[0]).strip():
                return str(values[0])
        elif ext in (".flac", ".ogg", ".opus"):
            f = MutagenFile(song)
            for key in ("syncedlyrics", "lyrics", "unsyncedlyrics", "unsynced lyrics"):
                values = f.tags.get(key) if f is not None and f.tags else None
                if values and str(values[0]).strip():
                    return str(values[0])
    except Exception:
        pass
    return ""


def lyrics_from_text(text: str, source: str, artist: str = "", title: str = "") -> Lyrics | None:
    lines, synced = parse_lyrics(text)
    if synced:
        lines = fold_layers(lines)
    if not any(l.text for l in lines):
        return None
    if len(lines) > MAX_LINES:
        raise LocalLyricsError(f"{source} has too many lines to be lyrics")
    return Lyrics(lines, synced, artist, title, source)


def lyrics_from_file(path: Path, artist: str = "", title: str = "") -> Lyrics:
    """The lyrics in a file you named yourself; problems are errors, not silence."""
    lyrics = lyrics_from_text(read_text_file(path), path.name, artist, title)
    if lyrics is None:
        raise LocalLyricsError(f"{path.name} has no lyrics in it")
    return lyrics


def local_lyrics(song: Path, artist: str = "", title: str = "") -> Lyrics | None:
    """
    Lyrics found next to or inside the song: its own .lrc (unless music-tools
    wrote it), else the tags. None if there aren't any or they can't be read.
    """
    try:
        side = song.with_suffix(".lrc")
        if side.is_file() and not made_by_us(side):
            found = lyrics_from_text(read_text_file(side), side.name, artist, title)
            if found:
                return found
        text = embedded_lyrics(song)
        if text.strip():
            return lyrics_from_text(text, "the song's tags", artist, title)
    except (OSError, LocalLyricsError):
        pass
    return None
