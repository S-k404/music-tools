"""Turn translated lyrics into what the lyrics tool shows and writes: an HTML page,
an .lrc/.txt file for music players, and a side-by-side view for the terminal."""

import html
import json
import re
import urllib.parse
from dataclasses import dataclass

from common import bold, dim, green, text_width, violet


@dataclass
class Line:
    time: float | None   # seconds into the song; None when the lyrics have no timestamps
    text: str            # the original line; "" is a gap or a break between verses
    romaji: str = ""
    english: str = ""


# What each line can show. Any combination can be kept, e.g. original + English, or romanization + English.
LAYERS = ("original", "romanization", "english")
LAYER_ATTR = {"original": "text", "romanization": "romaji", "english": "english"}
LAYER_CODES = {"original": "o", "romanization": "r", "english": "e"}
_LAYER_ALIASES = {
    "original": "original", "orig": "original", "text": "original", "native": "original", "source": "original",
    "kanji": "original", "hangul": "original", "hanzi": "original", "cyrillic": "original", "script": "original",
    "romanization": "romanization", "romanisation": "romanization", "roman": "romanization",
    "romaji": "romanization", "pinyin": "romanization", "rom": "romanization",
    "english": "english", "translation": "english", "translated": "english", "en": "english", "eng": "english",
}


def parse_layers(value) -> list:
    """'original,english', 'kanji + romaji' or a list -> canonical layer names, in display order.
    Raises ValueError with a message that says what's allowed."""
    names = re.split(r"[,+/\s]+", value) if isinstance(value, str) else list(value)
    chosen = set()
    for name in names:
        name = str(name).strip().lower()
        if not name:
            continue
        if name not in _LAYER_ALIASES:
            raise ValueError(f"unknown layer {name!r}: use original (kanji/hangul), romanization (romaji), english")
        chosen.add(_LAYER_ALIASES[name])
    if not chosen:
        raise ValueError("pick at least one layer: original, romanization, english")
    return [l for l in LAYERS if l in chosen]


def layer_label(layers) -> str:
    return " + ".join({"original": "original", "romanization": "romanization", "english": "English"}[l] for l in layers)


def _shown(line, layers) -> list:
    """The texts of this line in the chosen layers (the original if none of them has anything for it)."""
    parts = [getattr(line, LAYER_ATTR[l]) for l in layers if getattr(line, LAYER_ATTR[l])]
    return parts or ([line.text] if line.text else [])


LANGUAGE_NAMES = {
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "hi": "Hindi",
    "th": "Thai", "tr": "Turkish", "id": "Indonesian", "vi": "Vietnamese", "nl": "Dutch", "sv": "Swedish",
    "pl": "Polish", "el": "Greek", "he": "Hebrew", "fa": "Persian", "tl": "Filipino", "fil": "Filipino",
    "bn": "Bengali", "ta": "Tamil", "ms": "Malay", "ro": "Romanian", "cs": "Czech", "hu": "Hungarian",
    "fi": "Finnish", "da": "Danish", "no": "Norwegian", "la": "Latin", "sw": "Swahili", "af": "Afrikaans",
}
ROMANIZATION_NAMES = {"ja": "Romaji", "ko": "Romanization", "zh": "Pinyin", "ru": "Romanization"}


def language_name(code: str) -> str:
    code = (code or "").split("-")[0].lower()
    return LANGUAGE_NAMES.get(code, code.upper() if code and code != "und" else "Unknown language")


def romanization_name(code: str) -> str:
    return ROMANIZATION_NAMES.get((code or "").split("-")[0].lower(), "Romanization")


def timestamp(seconds: float) -> str:
    """mm:ss.xx, the format .lrc files use."""
    centis = int(round(max(0.0, seconds) * 100))
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{minutes:02d}:{secs:02d}.{centis:02d}"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


# ---------------------------------------------------------------- .lrc / .txt for music players
def render_lrc(lines: list, title: str = "", artist: str = "", layers=LAYERS) -> str:
    """
    Synced lyrics: every line is followed by its romanization and translation at
    the same timestamp, which is how bilingual .lrc files are usually made.
    `layers` picks which of original / romanization / english are written.
    """
    out = []
    if title:
        out.append(f"[ti:{_one_line(title)}]")
    if artist:
        out.append(f"[ar:{_one_line(artist)}]")
    out.append(f"[by:music-tools ({layer_label(layers).replace(' + ', ' / ')})]")
    for line in lines:
        if line.time is None:
            continue
        stamp = f"[{timestamp(line.time)}]"
        if not line.text:
            out.append(stamp)  # an empty line ends the previous one on screen
            continue
        out += [stamp + part for part in _shown(line, layers)]
    return "\n".join(out) + "\n"


def render_txt(lines: list, layers=LAYERS) -> str:
    """Lyrics without timestamps: original, romanization and English stacked, verses separated."""
    out = []
    for line in lines:
        if not line.text:
            if out and out[-1] != "":
                out.append("")
            continue
        out += _shown(line, layers) + [""]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- terminal
def _wrap(text: str, width: int) -> list:
    """Split text into rows no wider than `width` columns (CJK counts double)."""
    rows, row, row_w = [], "", 0
    for token in _tokens(text):
        w = text_width(token)
        if row and row_w + w > width:
            rows.append(row.rstrip())
            row, row_w = "", 0
            token = token.lstrip()
            w = text_width(token)
        while w > width:  # one token wider than the column: cut it
            cut = ""
            for ch in token:
                if text_width(cut + ch) > width:
                    break
                cut += ch
            rows.append(cut)
            token = token[len(cut):]
            w = text_width(token)
        row += token
        row_w += w
    if row.strip():
        rows.append(row.rstrip())
    return rows or [""]


def _tokens(text: str):
    """Words (with their trailing space) for Latin text, single characters for CJK."""
    word = ""
    for ch in text:
        if text_width(ch) == 2:
            if word:
                yield word
                word = ""
            yield ch
        else:
            word += ch
            if ch == " ":
                yield word
                word = ""
    if word:
        yield word


def render_terminal(lines: list, width: int, indent: int = 2, layers=LAYERS) -> list:
    """The lyrics as side-by-side columns sized to the terminal (stacked when it's narrow)."""
    paint = {"original": lambda s: s, "romanization": violet, "english": green}
    columns = [(LAYER_ATTR[l], paint[l]) for l in layers if any(getattr(x, LAYER_ATTR[l]) for x in lines)]
    columns = columns or [("text", lambda s: s)]
    synced = any(l.time is not None for l in lines)
    time_w = 6 if synced else 0
    usable = max(20, width - indent * 2 - time_w)
    gap = 3
    out = []
    stacked = len(columns) > 1 and usable < 24 * len(columns)
    per_col = (usable - gap * (len(columns) - 1)) // len(columns)
    for line in lines:
        if not line.text:
            if out and out[-1] != "":
                out.append("")
            continue
        stamp = dim(f"{int(line.time // 60):02d}:{int(line.time % 60):02d}  ") if synced and line.time is not None \
            else " " * time_w
        values = [getattr(line, key) for key, _ in columns]
        if not any(values):
            values[0] = line.text
        if stacked:
            for i, (key, paint) in enumerate(columns):
                for j, row in enumerate(_wrap(values[i], usable)):
                    if row:
                        out.append(" " * indent + (stamp if i == 0 and j == 0 else " " * time_w)
                                   + (bold(row) if key == "text" else paint(row)))
            out.append("")
            continue
        cells = [_wrap(v, per_col) for v in values]
        for r in range(max(len(c) for c in cells)):
            parts = []
            for (key, paint), cell in zip(columns, cells):
                row = cell[r] if r < len(cell) else ""
                pad = " " * (per_col - text_width(row))
                parts.append((bold(row) if key == "text" else paint(row)) + pad if row else " " * per_col)
            out.append(" " * indent + (stamp if r == 0 else " " * time_w) + (" " * gap).join(parts).rstrip())
    while out and out[-1] == "":
        out.pop()
    return out


# ---------------------------------------------------------------- HTML page
_CSS = """
:root{--bg:#fbfaf8;--fg:#1d1b20;--dim:#7a7683;--rule:#e8e4ee;--accent:#7c4dff;--rom:#7a5cbf;--en:#0b7a5a;--now:#f1eaff}
@media (prefers-color-scheme:dark){:root{--bg:#141318;--fg:#ece9f2;--dim:#8f8a9b;--rule:#2a2732;--accent:#b39dff;--rom:#b7a3f5;--en:#6fd8b4;--now:#241f36}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.55 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans CJK JP","Malgun Gothic","PingFang SC",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px 80px}
header{padding:28px 0 12px}
h1{margin:0;font-size:1.5rem;line-height:1.25}
.meta{color:var(--dim);font-size:.9rem;margin-top:4px}
.bar{position:sticky;top:0;z-index:2;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--rule);display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center}
audio{flex:1 1 320px;min-width:0;height:36px}
label{font-size:.85rem;color:var(--dim);user-select:none;white-space:nowrap}
.presets{display:flex;flex-wrap:wrap;gap:6px}
button{font:inherit;font-size:.8rem;color:var(--fg);background:transparent;border:1px solid var(--rule);border-radius:999px;padding:3px 12px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
table{width:100%;border-collapse:collapse;margin-top:8px}
th{position:static;text-align:left;font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);padding:8px 10px;border-bottom:1px solid var(--rule)}
td{padding:6px 10px;vertical-align:top;border-bottom:1px solid var(--rule)}
td.t{width:4.2rem;color:var(--dim);font-variant-numeric:tabular-nums;font-size:.85rem;white-space:nowrap}
td.r{color:var(--rom)}td.e{color:var(--en)}
tr.gap td{border:0;padding:6px}
tr[data-t]{cursor:pointer}
tr.now td{background:var(--now)}
tr.now td.o{font-weight:600}
body.hide-o .o,body.hide-r .r,body.hide-e .e{display:none}
@media (max-width:720px){
  table,tbody,tr,td{display:block}thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:6px 0}td{border:0;padding:1px 10px}td.t{width:auto}
  tr.gap{border:0;padding:4px}
}
"""

_JS = """
const q=s=>[...document.querySelectorAll(s)];
const shown=(document.body.dataset.show||'o,r,e').split(',');
const apply=()=>q('input[data-col]').forEach(c=>document.body.classList.toggle('hide-'+c.dataset.col,!c.checked));
for(const c of q('input[data-col]')){
  let saved=null;try{saved=localStorage.getItem('lyrics-hide-'+c.dataset.col)}catch(e){}
  c.checked=saved===null?shown.includes(c.dataset.col):saved!=='1';
  c.addEventListener('change',()=>{apply();try{localStorage.setItem('lyrics-hide-'+c.dataset.col,c.checked?'0':'1')}catch(e){}});
}
apply();
for(const b of q('button[data-cols]')){
  b.addEventListener('click',()=>{const on=b.dataset.cols.split(',');
    for(const c of q('input[data-col]')){c.checked=on.includes(c.dataset.col);c.dispatchEvent(new Event('change'))}});
}
const audio=document.getElementById('a'),rows=q('tr[data-t]');
if(audio&&rows.length){
  let now=-1;
  const times=rows.map(r=>parseFloat(r.dataset.t));
  audio.addEventListener('timeupdate',()=>{
    let cur=-1;for(let i=0;i<times.length;i++){if(times[i]<=audio.currentTime+0.15)cur=i;else break}
    if(cur===now)return;
    if(now>=0)rows[now].classList.remove('now');
    if(cur>=0){rows[cur].classList.add('now');
      if(!rows[cur].classList.contains('gap'))rows[cur].scrollIntoView({block:'center',behavior:'smooth'})}
    now=cur;
  });
  rows.forEach((r,i)=>r.addEventListener('click',()=>{audio.currentTime=times[i];audio.play().catch(()=>{})}));
}
"""

_DATA_RE = re.compile(r'<script type="application/json" id="lyrics-data">(.*?)</script>', re.DOTALL)


def _data_block(lines, title, artist, lang, source) -> str:
    """All the lyrics data, stored in the page itself so the layers can be switched later without asking anyone."""
    data = {"version": 1, "title": title, "artist": artist, "lang": lang, "source": source,
            "lines": [[l.time, l.text, l.romaji, l.english] for l in lines]}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="lyrics-data">{blob}</script>'


def parse_html_data(page: str):
    """The lyrics stored in a page written by render_html: {'lines': [Line], 'title', 'artist', 'lang', 'source'}, or None."""
    m = _DATA_RE.search(page)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        lines = [Line(t, str(text), str(rom), str(eng)) for t, text, rom, eng in data["lines"]]
    except (ValueError, KeyError, TypeError):
        return None
    return {"lines": lines, "title": data.get("title", ""), "artist": data.get("artist", ""),
            "lang": data.get("lang", ""), "source": data.get("source", "")}


def render_html(lines: list, title: str, artist: str = "", lang: str = "", source: str = "",
                audio_href: str = "", layers=LAYERS) -> str:
    """
    One self-contained page with the lyrics in columns; plays along with the song if they're synced.
    Every layer is in the page, so it can be switched live (original + English, romanization + English,
    original + romanization, or all three); `layers` only says which are showing when it first opens.
    """
    synced = any(l.time is not None for l in lines)
    has_r = any(l.romaji for l in lines)
    has_e = any(l.english for l in lines)
    esc = html.escape
    romanized = "-latn" in (lang or "").lower()
    head = " · ".join(x for x in (artist, language_name(lang) + (" (romanized)" if romanized else "") + " → English"
                                  if lang else "") if x)
    lang_attr = esc(lang or "") or "und"
    rom_name = esc(romanization_name(lang))

    cells = ['<th class="t">Time</th>'] if synced else []
    cells += ['<th class="o">Original</th>']
    if has_r:
        cells.append(f'<th class="r">{rom_name}</th>')
    if has_e:
        cells.append('<th class="e">English</th>')
    ncols = len(cells)

    rows = []
    previous_blank = True
    for line in lines:
        attr = f' data-t="{line.time:.2f}"' if line.time is not None else ""
        if not line.text:
            if not previous_blank or line.time is not None:
                rows.append(f'<tr class="gap"{attr}><td colspan="{ncols}"></td></tr>')
            previous_blank = True
            continue
        previous_blank = False
        tds = []
        if synced:
            mins, secs = divmod(int(line.time or 0), 60)
            tds.append(f'<td class="t">{mins}:{secs:02d}</td>')
        tds.append(f'<td class="o" lang="{lang_attr}">{esc(line.text)}</td>')
        if has_r:
            tds.append(f'<td class="r">{esc(line.romaji)}</td>')
        if has_e:
            tds.append(f'<td class="e" lang="en">{esc(line.english)}</td>')
        rows.append(f"<tr{attr}>{''.join(tds)}</tr>")

    toggles = ['<label><input type="checkbox" data-col="o" checked> Original</label>']
    if has_r:
        toggles.append(f'<label><input type="checkbox" data-col="r" checked> {rom_name}</label>')
    if has_e:
        toggles.append('<label><input type="checkbox" data-col="e" checked> English</label>')
    available = ["o"] + (["r"] if has_r else []) + (["e"] if has_e else [])
    presets = [("All", "o,r,e"), ("Original + English", "o,e"), ("Romanization + English", "r,e"),
               ("Original + Romanization", "o,r")]
    # one-click pairs, only when all three layers exist (with two, the checkboxes are enough)
    buttons = "".join(f'<button type="button" data-cols="{codes}">{name}</button>' for name, codes in presets) \
        if len(available) == 3 else ""
    show = ",".join(LAYER_CODES[l] for l in layers if LAYER_CODES[l] in available) or "o"
    player = (f'<audio id="a" controls preload="metadata" src="{esc(urllib.parse.quote(audio_href))}"></audio>'
              if synced and audio_href else "")
    note = "Synced: press play and the current line lights up; click any line to jump there." if player else \
        ("Time-synced lyrics" if synced else "These lyrics have no timestamps.")
    source_note = f" · lyrics from {esc(source)}" if source else ""
    presets_html = f'<div class="presets">{buttons}</div>' if buttons else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="generator" content="music-tools">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{_CSS}</style>
</head>
<body data-show="{show}">
<div class="wrap">
<header>
<h1>{esc(title)}</h1>
<div class="meta">{esc(head)}{source_note}</div>
<div class="meta">{esc(note)}</div>
</header>
<div class="bar">{player}{presets_html}{''.join(toggles)}</div>
<table>
<thead><tr>{''.join(cells)}</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
</div>
{_data_block(lines, title, artist, lang, source)}
<script>{_JS}</script>
</body>
</html>
"""
