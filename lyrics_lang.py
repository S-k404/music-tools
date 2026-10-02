"""
Which language lyrics are in, worked out from the writing itself, so it works
offline and costs no web request. Only Latin-script text that could be Spanish,
French, romanized Japanese and so on is left for a translation service to identify.
"""

import re
import unicodedata
from collections import Counter

from lyrics_render import Line

FOREIGN_SHARE = 0.15   # this much non-Latin writing makes a song "foreign"
ENGLISH_SHARE = 0.25   # this many common English words makes Latin text "English"
ENGLISH_WORDS = frozenset(
    "the a an and or but if of to in on at for with from by as is are was were be been am i you he she it we "
    "they me my your his her its our their this that these those not no yes so do does did just like up out all "
    "what when who how will can got get gonna wanna oh yeah baby love know now never ever more one there here "
    "say too then than into over back down".split())


def script_of(ch: str) -> str:
    """'kana', 'hangul', 'han', 'cyrillic', 'latin' or 'other' for a letter; '' for anything that isn't a letter."""
    o = ord(ch)
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
        return "kana"
    if 0xAC00 <= o <= 0xD7FF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x0400 <= o <= 0x04FF:
        # the whole Cyrillic-script family (Russian, Ukrainian, Bulgarian, ...), treated as Russian
        # below: the library this was built against has no other Cyrillic-script songs
        return "cyrillic"
    if not ch.isalpha():
        return ""
    return "latin" if o < 0x0250 or 0x1E00 <= o <= 0x1EFF else "other"


def script_counts(text: str) -> Counter:
    # NFC first: macOS stores Korean decomposed, which would otherwise count as jamo
    return Counter(s for s in map(script_of, unicodedata.normalize("NFC", text)) if s)


def has_letters(text: str) -> bool:
    return any(c.isalpha() for c in text)


def norm(text: str) -> str:
    """Letters and digits only, lowercase: for asking 'is this the same text?'"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def guess_language(text: str) -> str:
    """'ja', 'ko', 'zh', 'ru' or 'en' when the writing makes it clear, else 'und' (a translation service must say)."""
    c = script_counts(text)
    letters = sum(c.values())
    if not letters:
        return "en"  # nothing but symbols and sounds: nothing to translate
    if (letters - c["latin"]) / letters >= FOREIGN_SHARE:
        if c["hangul"] > c["kana"] + c["han"]:
            return "ko"
        if c["kana"]:
            return "ja"  # Japanese always has some kana; Chinese has none
        if c["cyrillic"] and c["cyrillic"] >= c["other"]:
            return "ru"
        return "zh" if c["han"] and c["han"] >= c["other"] else "und"
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())
    hits = sum(w in ENGLISH_WORDS for w in words)
    return "en" if words and hits / len(words) >= ENGLISH_SHARE else "und"


def line_language(text: str, song_lang: str) -> str:
    """Which romanizer suits one line ('' for none). Kanji on their own follow the song's language."""
    c = script_counts(text)
    if c["hangul"]:
        return "ko"
    if c["kana"]:
        return "ja"
    if c["han"]:
        return song_lang if song_lang in ("ja", "zh") else ""
    if c["cyrillic"]:
        return "ru"
    return ""


# ---------------------------------------------------------------- bilingual .lrc files
def classify_layer(texts: list) -> str:
    """What a second line at the same timestamp is: 'english', 'romanization' (Latin letters, no accents) or 'other'."""
    joined = "\n".join(texts)
    if guess_language(joined) == "en":
        return "english"
    letters = [c for c in joined if c.isalpha()]
    plain = sum(c.isascii() or c in "āīūēōĀĪŪĒŌ" for c in letters)
    if letters and plain / len(letters) >= 0.97 and guess_language(joined) == "und":
        return "romanization"
    return "other"  # a translation into some other language (Vietnamese, Chinese, ...): not usable


def fold_layers(lines: list) -> list:
    """
    Bilingual .lrc files put a translation or romanization on a second line with
    the same timestamp as the original. Fold those into one line each: an English
    or romanized second line becomes that layer, a line in any other language is
    dropped. Files that aren't like that come back unchanged.
    """
    groups = []
    for line in lines:
        if groups and line.time is not None and groups[-1][0].time == line.time:
            groups[-1].append(line)
        else:
            groups.append([line])
    spoken = [[l for l in g if l.text] for g in groups]
    talking = [g for g in spoken if g]
    layered = [g for g in talking if len(g) >= 2]
    if len(layered) < max(2, len(talking) / 2):
        return lines
    kinds = {}
    for k in range(1, max(len(g) for g in layered)):
        kinds[k] = classify_layer([g[k].text for g in layered if len(g) > k])
    out = []
    for group, texts in zip(groups, spoken):
        if not texts:
            out.extend(group)
            continue
        base = texts[0]
        for k, extra in enumerate(texts[1:], 1):
            if kinds.get(k) == "english" and not base.english:
                base.english = extra.text
            elif kinds.get(k) == "romanization" and not base.romaji:
                base.romaji = extra.text
        out.append(base)
    return out
