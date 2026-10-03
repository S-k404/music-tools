"""
Romanization for the lyrics tool: writing non-Latin lyrics in Latin letters,
alongside the original and the English translation.

Each language uses the best engine that's installed and falls back to a simpler
one, so a missing package lowers the quality instead of breaking the run:

  Japanese   cutlet (real word splitting: "wa" for the particle は, "wo" for を) -> pykakasi -> none
  Korean     korean-romanizer (applies pronunciation rules: 좋아 is "joa") -> a built-in
             letter-by-letter Revised Romanization that needs no package
  Chinese    pypinyin -> none
  Russian    a built-in letter-by-letter table, no package needed (Cyrillic letters are already
             single characters, unlike Hangul's composed syllable blocks, so there's nothing a
             package would meaningfully improve on for song lyrics)

`status()` says which engine each language ended up with, so the tool can tell
you when something is missing instead of quietly leaving the column empty.
"""

import importlib.util
import re
import threading
import unicodedata

from lyrics_lang import line_language, norm

# ---------------------------------------------------------------- Korean
_HANGUL_BASE = 0xAC00
_HANGUL_COUNT = 11172
_INITIALS = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "c", "k", "t", "p", "h"]
_VOWELS = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi",
          "yu", "eu", "yi", "i"]
_FINALS = ["", "g", "kk", "gs", "n", "nj", "nh", "d", "l", "lg", "lm", "lb", "ls", "lt", "lp", "lh", "m", "b",
          "bs", "s", "ss", "ng", "j", "c", "k", "t", "p", "h"]


def romanize_korean_letters(text: str) -> str:
    """Built-in fallback: every Hangul syllable block romanized in place, anything else untouched.
    Spelled out letter by letter, so it ignores how the words are actually pronounced."""
    out = []
    for ch in unicodedata.normalize("NFC", text):
        code = ord(ch) - _HANGUL_BASE
        if 0 <= code < _HANGUL_COUNT:
            initial, rest = divmod(code, 588)
            vowel, final = divmod(rest, 28)
            out.append(_INITIALS[initial] + _VOWELS[vowel] + _FINALS[final])
        else:
            out.append(ch)
    return "".join(out)


try:
    from korean_romanizer.romanizer import Romanizer as _KoreanRomanizer
except ImportError:
    _KoreanRomanizer = None


def romanize_korean(text: str) -> str:
    if _KoreanRomanizer:
        try:
            return _KoreanRomanizer(unicodedata.normalize("NFC", text)).romanize()
        except Exception:
            pass  # an odd line falls back to the plain algorithm rather than going without
    return romanize_korean_letters(text)


# ---------------------------------------------------------------- Japanese
_lock = threading.Lock()   # the tokenizers aren't guaranteed thread safe, and this is fast
_japanese = None           # (engine name, object); loaded on first use because cutlet takes a second or two


def _japanese_engine() -> tuple:
    global _japanese
    with _lock:
        if _japanese is None:
            _japanese = ("", None)
            try:
                import cutlet
                _japanese = ("cutlet", cutlet.Cutlet(use_foreign_spelling=False))
            except Exception:
                try:
                    import pykakasi
                    _japanese = ("pykakasi", pykakasi.kakasi())
                except Exception:
                    pass
        return _japanese


def romanize_japanese(text: str) -> str:
    name, engine = _japanese_engine()
    with _lock:
        if name == "cutlet":
            return engine.romaji(text)
        if name == "pykakasi":
            return " ".join(item["hepburn"] for item in engine.convert(text) if item["hepburn"])
    return ""


# ---------------------------------------------------------------- Chinese
_pinyin_lock = threading.Lock()
_pinyin_engine = None   # (pinyin function, Style) once loaded, False if pypinyin isn't installed; loaded on first
                        # use because importing pypinyin takes ~150 ms, and most tools never romanize Chinese


def _pinyin():
    global _pinyin_engine
    with _pinyin_lock:
        if _pinyin_engine is None:
            try:
                from pypinyin import Style, pinyin
                _pinyin_engine = (pinyin, Style)
            except ImportError:
                _pinyin_engine = False
        return _pinyin_engine


def _pinyin_installed() -> bool:
    return importlib.util.find_spec("pypinyin") is not None

_ZH_PUNCT = str.maketrans({"，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";", "、": ",", "（": "(", "）": ")"})


def romanize_chinese(text: str) -> str:
    engine = _pinyin()
    if not engine:
        return ""
    pinyin, Style = engine
    parts = (syllable[0].strip() for syllable in pinyin(text, style=Style.TONE, errors="default") if syllable)
    return " ".join(p for p in parts if p).translate(_ZH_PUNCT)


# ---------------------------------------------------------------- Russian
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def romanize_russian(text: str) -> str:
    """Every Cyrillic letter romanized in place, anything else untouched. ъ and ь (silent on their
    own) are dropped rather than written as an apostrophe, which reads as noisy punctuation next to
    the English column."""
    out = []
    for ch in unicodedata.normalize("NFC", text):
        mapped = _CYRILLIC.get(ch.lower())
        if mapped is None:
            out.append(ch)
        elif ch.isupper() and mapped:
            out.append(mapped[0].upper() + mapped[1:])
        else:
            out.append(mapped)
    return "".join(out)


# ---------------------------------------------------------------- putting it together
ROMANIZERS = {"ko": romanize_korean, "ja": romanize_japanese, "zh": romanize_chinese, "ru": romanize_russian}
SUPPORTED = tuple(ROMANIZERS)   # the one place "which languages get romanized" is declared


def _code(lang: str) -> str:
    return (lang or "").split("-")[0].lower()


def available(lang: str) -> bool:
    """Whether lyrics in this language can be romanized right now (a package may be missing)."""
    code = _code(lang)
    if code == "ko":
        return True
    if code == "ja":
        return bool(_japanese_engine()[0])
    if code == "zh":
        return _pinyin_installed()
    if code == "ru":
        return True
    return False


def status() -> dict:
    """Which engine each language uses ('' = none installed), e.g. {'ja': 'cutlet', 'ko': 'korean-romanizer', 'zh': 'pypinyin'}."""
    return {"ja": _japanese_engine()[0],
            "ko": "korean-romanizer" if _KoreanRomanizer else "built-in",
            "zh": "pypinyin" if _pinyin_installed() else "",
            "ru": "built-in"}


def _tidy(text: str) -> str:
    return re.sub(r"\s+([,.!?;:)])", r"\1", " ".join(unicodedata.normalize("NFC", text).split()))


def romanize(lines: list, lang: str) -> bool:
    """
    Fill in `line.romaji` for every line that has Japanese, Korean, Chinese or Russian
    writing (a line of plain English inside a foreign-language song is left alone).
    Returns whether anything was filled in.
    """
    code = _code(lang)
    filled = False
    for line in lines:
        if not line.text:
            continue
        if line.romaji:  # the file already came with one: keep it
            filled = True
            continue
        fn = ROMANIZERS.get(line_language(line.text, code))
        if not fn or not available(line_language(line.text, code)):
            continue
        try:
            result = _tidy(fn(line.text))
        except Exception:
            result = ""  # one odd line just goes without
        line.romaji = result if result and norm(result) != norm(line.text) else ""
        filled = filled or bool(line.romaji)
    return filled
