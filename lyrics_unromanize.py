"""
Romaji back into hiragana, so lyrics that were only ever written in Latin letters can be translated.

Translators such as Google and DeepL hand romanized Japanese back unchanged, but read the same words written
in kana very well ("Yume naraba dore hodo yokatta deshou" becomes "How good would it be if it were a dream?").
Kanji can't be recovered, so this loses which word a sound meant, but lyrics are mostly common words.

Korean romanization and pinyin can't be turned back reliably (the spelling follows pronunciation, so many
hangul/hanzi map to the same letters); those are only translated by services that read them directly (Claude).
"""

import re
import unicodedata

_ROWS = {
    "k": "かきくけこ", "s": "さすせそ", "t": "たてと", "n": "なにぬねの", "h": "はひへほ", "m": "まみむめも",
    "r": "らりるれろ", "g": "がぎぐげご", "z": "ざずぜぞ", "d": "だでど", "b": "ばびぶべぼ", "p": "ぱぴぷぺぽ",
}
_TABLE = {"a": "あ", "i": "い", "u": "う", "e": "え", "o": "お", "ya": "や", "yu": "ゆ", "yo": "よ", "wa": "わ",
          "wo": "を", "wi": "うぃ", "we": "うぇ", "n": "ん"}
for _consonant, _kana in _ROWS.items():
    if len(_kana) == 5:
        for _v, _k in zip("aiueo", _kana):
            _TABLE[_consonant + _v] = _k
_TABLE.update({
    "sa": "さ", "shi": "し", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "ta": "た", "chi": "ち", "ti": "ち", "tsu": "つ", "tu": "つ", "te": "て", "to": "と",
    "ha": "は", "hi": "ひ", "fu": "ふ", "hu": "ふ", "he": "へ", "ho": "ほ",
    "za": "ざ", "ji": "じ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
    "da": "だ", "di": "ぢ", "du": "づ", "de": "で", "do": "ど",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "she": "しぇ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "che": "ちぇ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "je": "じぇ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "zya": "じゃ", "zyu": "じゅ", "zyo": "じょ",
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
})
for _c, _small in (("k", "き"), ("g", "ぎ"), ("n", "に"), ("h", "ひ"), ("m", "み"), ("r", "り"), ("b", "び"), ("p", "ぴ")):
    for _v, _y in (("a", "ゃ"), ("u", "ゅ"), ("o", "ょ")):
        _TABLE[_c + "y" + _v] = _small + _y
_MACRONS = str.maketrans({"ā": "aa", "ī": "ii", "ū": "uu", "ē": "ee", "ō": "ou",
                          "Ā": "aa", "Ī": "ii", "Ū": "uu", "Ē": "ee", "Ō": "ou"})
_TOKEN = re.compile(r"^([^A-Za-zāīūēōĀĪŪĒŌ]*)([A-Za-zāīūēōĀĪŪĒŌ'\-]+)([^A-Za-zāīūēōĀĪŪĒŌ]*)$")
_PARTICLES = {"wa": "は", "e": "へ", "wo": "を", "o": "を"}   # spoken "wa"/"e"/"o" written as particles


def _word(word: str):
    """One romaji word as hiragana, or None if it isn't (entirely) romaji, e.g. an English word."""
    w = word.lower().translate(_MACRONS)
    out, i = [], 0
    while i < len(w):
        ch = w[i]
        if ch in "'-":  # n' separates ん from a following vowel; a hyphen is the long-vowel mark
            out.append("ー" if ch == "-" and out else "")
            i += 1
            continue
        if ch == "t" and w[i + 1:i + 3] == "ch":            # matcha: the small tsu doubles the "ch"
            out.append("っ")
            i += 1
            continue
        if i + 1 < len(w) and ch == w[i + 1] and ch not in "aiueon":
            out.append("っ")
            i += 1
            continue
        if ch == "n" and (i + 1 == len(w) or w[i + 1] not in "aiueoy" or w[i + 1:i + 2] == "'"):
            out.append("ん")
            i += 1
            continue
        for size in (3, 2, 1):
            if w[i:i + size] in _TABLE:
                out.append(_TABLE[w[i:i + size]])
                i += size
                break
        else:
            return None
    return "".join(out)


def romaji_to_kana(line: str) -> str:
    """A line of romanized Japanese as hiragana. Anything that isn't romaji (English words, numbers,
    punctuation) is left as it is, and a line that is mostly not romaji (plain English) is returned unchanged."""
    pieces, converted, words, foreign = [], 0, 0, 0
    for token in line.split():
        m = _TOKEN.match(unicodedata.normalize("NFC", token))
        if not m:
            pieces.append(token)
            continue
        before, core, after = m.groups()
        kana = _PARTICLES.get(core.lower()) or _word(core)
        words += 1
        converted += kana is not None
        foreign += kana is None and len(core) >= 3
        pieces.append(before + (kana if kana is not None else core) + after)
    # "Baby tell me why" or "I love you so much": short words there happen to be valid romaji ("me", "so", "you"),
    # so a line with several real non-romaji words is English, and is left alone
    if foreign >= 2 and (words - converted) / words >= 0.4:
        return line
    return "".join(pieces)
