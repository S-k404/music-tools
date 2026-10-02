"""The look of the lyrics tool in the terminal: a banner, and a cat that keeps busy while songs are processed."""

import os
import re
import shutil
import sys
import threading

from common import COLOR, bold, dim, fit, text_width, tqdm

# The cat only moves on a real terminal; MUSIC_TOOLS_NO_ANIMATION=1 keeps it still, NO_COLOR=1 drops the colours.
ANIMATE = sys.stdout.isatty() and not os.environ.get("MUSIC_TOOLS_NO_ANIMATION")

_LETTERS = {
    "L": ("╦  ", "║  ", "╩═╝"), "Y": ("╦ ╦", "╚╦╝", " ╩ "), "R": ("╦═╗", "╠╦╝", "╩╚═"),
    "I": ("╦", "║", "╩"), "C": ("╔═╗", "║  ", "╚═╝"), "S": ("╔═╗", "╚═╗", "╚═╝"),
}
_BANNER = ["".join(_LETTERS[ch][row] + " " for ch in "LYRICS").rstrip() for row in range(3)]
_GRADIENT = [205, 205, 170, 170, 135, 135, 99, 99, 69, 69, 75, 75, 81, 81]  # pink -> violet -> blue -> cyan
TAGLINE = "original + romaji + English, side by side"
CAT_WIDTH = 13
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _paint(code: int, text: str) -> str:
    return f"\x1b[38;5;{code}m{text}\x1b[0m" if COLOR else text


def cat(tick: int = 0, mood: str = "listen") -> list:
    """
    A cat, 3 rows tall. Moods: listen (notes drift by), happy, sad, sleep.
    It blinks now and then and flicks its tail; `tick` is the animation frame.
    """
    beat = tick % 36
    if mood == "happy":
        eyes, extra = "^.^", ["♥   ", " ♥  ", "  ♥ ", "   ♥"][tick % 4]
    elif mood == "sad":
        eyes, extra = ";.;", "    "
    elif mood == "sleep":
        eyes, extra = "-.-", ["z   ", " zZ ", "  Z ", "  zZ"][(tick // 2) % 4]
    else:
        eyes = "-.-" if beat in (16, 17) else ("^.^" if beat in (28, 29, 30) else "o.o")
        extra = ["♪   ", " ♪  ", "  ♫ ", "   ♪"][tick % 4]
    tail = "  ~" if (tick // 2) % 2 else " ~ "
    top, mid, low = f" /\\_/\\  {extra}", f"( {eyes} )", f" > ^ <{tail}"
    return [_paint(117, top[:7]) + _paint(205, top[7:]),
            _paint(117, mid),
            _paint(117, low[:6]) + _paint(183, low[6:])]


def mini_cat(tick: int, mood: str = "listen") -> str:
    """A one-line cat for the progress bar."""
    face = {"happy": "=^o^=", "sad": "=;.;=", "sleep": "=-.-="}.get(mood)
    if face is None:
        face = "=-.-=" if tick % 30 in (14, 15) else "=^.^="
    tail = "~" if (tick // 2) % 2 else " "
    note = "♪♫"[(tick // 3) % 2] if mood == "listen" else " "
    return _paint(117, face) + _paint(183, tail) + " " + _paint(205, note)


def width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def banner(tick: int = 0) -> list:
    """The LYRICS title with a cat next to it (just a small title on narrow terminals)."""
    w = width()
    if w < len(_BANNER[0]) + 6:
        return [bold("♫ lyrics"), dim(TAGLINE[: max(0, w - 4)])]
    lines = []
    for row in _BANNER:
        step = len(row) / len(_GRADIENT)
        lines.append("  " + ("".join(_paint(_GRADIENT[min(int(i / step), len(_GRADIENT) - 1)], ch)
                                     for i, ch in enumerate(row)) if COLOR else row))
    if w >= len(_BANNER[0]) + CAT_WIDTH + 10:
        pad = " " * 4
        for i, part in enumerate(cat(tick)):
            lines[i] += pad + part
    lines += ["  " + dim("─" * (len(_BANNER[0]) - 2)), f"  {_paint(205, '♫')} {dim(TAGLINE)}"]
    return lines


def show_banner() -> None:
    print()
    for line in banner():
        print(line)
    print()


def cat_says(message: str, mood: str = "happy") -> list:
    """A cat with a speech bubble; without room for the cat it's just the message."""
    w = width()
    room = w - CAT_WIDTH - 8
    if room < 16:
        return ["  " + message]
    message = fit(message, min(text_width(message), room)).rstrip()
    inner = text_width(message)
    art = cat(0, mood)
    bubble = ["╭" + "─" * (inner + 2) + "╮", "│ " + message + " │", "╰" + "─" * (inner + 2) + "╯"]
    pad = [" " * (CAT_WIDTH - len(_ANSI.sub("", a))) for a in art]  # rows differ in width, the bubble must line up
    return ["  " + a + p + "  " + dim(b) for a, p, b in zip(art, pad, bubble)]


def fact(label: str, value: str) -> None:
    print(f"  {dim(fit(label, 11))} {value}")


class CatProgress:
    """
    A progress bar with a cat that keeps busy while songs are processed, and
    the song it's on. On a terminal that can't animate it prints nothing;
    the tool's report at the end says everything.
    """

    def __init__(self, total: int, desc: str = "Working", quiet: bool = False):
        self.total, self.desc = total, desc
        self.enabled = bool(tqdm) and sys.stdout.isatty() and not quiet and total > 0
        self.label, self.mood, self.tick = "", "listen", 0
        self._bar, self._stop, self._thread = None, threading.Event(), None

    def __enter__(self):
        if self.enabled:
            self._bar = tqdm(total=self.total, unit="song", dynamic_ncols=True, leave=False,
                             bar_format="  {desc}  {bar:22} {percentage:3.0f}%  {n_fmt}/{total_fmt}  {elapsed}<{remaining}  {postfix}")
            self._draw()
            if ANIMATE:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._bar:
            self._bar.close()

    def _draw(self):
        room = max(10, width() - 62)
        self._bar.set_description_str(f"{mini_cat(self.tick, self.mood)} {self.desc}", refresh=False)
        self._bar.set_postfix_str(dim(fit(self.label, room).rstrip()) if self.label else "", refresh=False)
        self._bar.refresh()

    def _run(self):
        while not self._stop.wait(0.25):
            self.tick += 1
            try:
                self._draw()
            except Exception:  # a resize or closed terminal must never break the run
                return

    def start(self, label: str) -> None:
        self.label = label

    def advance(self) -> None:
        if self._bar:
            self._bar.update(1)
