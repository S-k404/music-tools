"""Interactive menu for music-tools: arrow keys to move, Enter to choose, q to go back."""

import copy
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import (ANSI, CHOICES, COLOR, DEFAULTS, HERE, IS_WINDOWS, LOG_DIR, bold, clean_path, config_path, cyan,
                    dim, green, open_path, read_raw_config, red, resolve, save_config, shell_quote, text_width,
                    yellow)

try:
    import termios
    import tty
    import select
    _TERMIOS_AVAILABLE = True
except ImportError:  # Windows
    _TERMIOS_AVAILABLE = False
try:
    import msvcrt
except ImportError:  # macOS, Linux
    msvcrt = None


def raw_mode() -> bool:
    try:
        return (_TERMIOS_AVAILABLE or msvcrt is not None) and sys.stdin.isatty() and sys.stdout.isatty() and hasattr(sys.stdin, "fileno")
    except Exception:
        return False


try:
    import readline
except ImportError:
    readline = None

FAILED_LIST = HERE / "failed_album_art.txt"
SCRIPTS = {"art": "fix_album_art.py", "artists": "find_artist_art.py", "lyrics": "find_lyrics.py",
          "tags": "fix_misidentified_tags.py", "duplicates": "find_duplicates.py", "stats": "library_stats.py",
          "layout": "library_layout.py",
          "organize": "organize_music.py", "all": "run_all.py"}

# ---------------------------------------------------------------- styling
ANIMATE = not os.environ.get("MUSIC_TOOLS_NO_ANIMATION")
FRAME_SECONDS = 0.14


# "MUSIC TOOLS" in box-drawing letters, 3 rows tall
_LETTERS = {
    "M": ("╔╦╗", "║║║", "╩ ╩"), "U": ("╦ ╦", "║ ║", "╚═╝"), "S": ("╔═╗", "╚═╗", "╚═╝"),
    "I": ("╦", "║", "╩"), "C": ("╔═╗", "║  ", "╚═╝"), "T": ("╔╦╗", " ║ ", " ╩ "),
    "O": ("╔═╗", "║ ║", "╚═╝"), "L": ("╦  ", "║  ", "╩═╝"), " ": ("  ", "  ", "  "),
}
_BANNER = ["".join(_LETTERS[ch][row] for ch in "MUSIC TOOLS") for row in range(3)]
# pink -> violet -> blue -> cyan (256-colour palette)
_GRADIENT = [205, 205, 170, 170, 135, 135, 99, 99, 69, 69, 75, 75, 81, 81, 87, 87]
TAGLINE = "cover art + artist pictures + tag fixer"


def cat(tick: int) -> list:
    """A cat listening to music: tail flicks, notes drift, blinks now and then."""
    beat = tick % 36  # one blink and one smile every ~5 seconds
    eyes = "-.-" if beat in (16, 17) else ("^.^" if beat in (28, 29, 30) else "o.o")
    notes = ["♪   ", " ♪  ", "  ♫ ", "   ♪"][tick % 4]
    tail = "  ~" if (tick // 2) % 2 else " ~ "
    lines = [f" /\\_/\\  {notes}", f"( {eyes} )", f" > ^ <{tail}"]
    if not COLOR:
        return lines
    return [f"\x1b[38;5;117m{lines[0][:7]}\x1b[38;5;205m{lines[0][7:]}\x1b[0m",
            f"\x1b[38;5;117m{lines[1]}\x1b[0m",
            f"\x1b[38;5;117m{lines[2][:6]}\x1b[38;5;183m{lines[2][6:]}\x1b[0m"]


CAT_WIDTH = 13


def banner(tick=None) -> list:
    width = shutil.get_terminal_size((80, 24)).columns
    if width < len(_BANNER[0]) + 6:
        return [bold(cyan("♫ music-tools")), dim(TAGLINE[: max(0, width - 2)])]
    lines = []
    for row in _BANNER:
        if COLOR:
            step = len(row) / len(_GRADIENT)
            row = "".join(f"\x1b[38;5;{_GRADIENT[min(int(i / step), len(_GRADIENT) - 1)]}m{ch}"
                          for i, ch in enumerate(row)) + "\x1b[0m"
        lines.append("  " + row)
    if tick is not None and width >= len(_BANNER[0]) + CAT_WIDTH + 8:
        for i, part in enumerate(cat(tick)):
            lines[i] += "    " + part
    note = "\x1b[38;5;205m♫\x1b[0m" if COLOR else "♫"
    rule = "─" * (len(_BANNER[0]) - 2)
    lines += ["  " + dim(rule), f"  {note} {dim(TAGLINE)}"]
    return lines


def show_cursor(on: bool):
    if ANSI:
        sys.stdout.write("\x1b[?25h" if on else "\x1b[?25l")
        sys.stdout.flush()


def clear():
    if ANSI:
        sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")
        sys.stdout.flush()


# ---------------------------------------------------------------- input
def read_key(timeout=None):
    """Return the next key, or None if timeout (seconds) passes first."""
    return _read_key_posix(timeout) if _TERMIOS_AVAILABLE else _read_key_windows(timeout)


def _read_key_windows(timeout):
    if timeout is not None:
        deadline = time.monotonic() + timeout
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # arrows and function keys come as a prefix, then a code
        return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(msvcrt.getwch(), "")
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\x1b":
        return "esc"
    if ch in ("\r", "\n"):
        return "enter"
    return "space" if ch == " " else ch


def _read_key_posix(timeout):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if select.select([fd], [], [], 0.05)[0]:
                seq = os.read(fd, 2)
                return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left"}.get(seq, "esc")
            return "esc"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == b"\x03":
        raise KeyboardInterrupt
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    return ch.decode(errors="ignore")


_ANSI_CODES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HINT_COLUMN_MAX = 36   # a very long label doesn't push every hint off a narrow screen


def _shown_len(text: str) -> int:
    return text_width(_ANSI_CODES.sub("", text))


def _fit_line(line: str, width: int) -> str:
    """Cut a line (colour codes and all) to `width` columns, ending in … when something was cut, so a long hint or
    path never wraps and throws off the redraw."""
    if _shown_len(line) <= width:
        return line
    out, used, i = [], 0, 0
    while i < len(line):
        code = _ANSI_CODES.match(line, i)
        if code:
            out.append(code.group(0))
            i = code.end()
            continue
        w = text_width(line[i])
        if used + w > width - 1:
            break
        out.append(line[i])
        used += w
        i += 1
    return "".join(out) + "…" + ("\x1b[0m" if ANSI else "")


def _window(total: int, cursor: int, room: int) -> tuple:
    """(first, last+1) of the rows to show so the cursor row is visible when only `room` of `total` fit."""
    if total <= room:
        return 0, total
    first = min(max(0, cursor - room // 2), total - room)
    return first, first + room


def _render(title, header, rows, cursor, footer, big=False, tick=None, redraw=False):
    size = shutil.get_terminal_size((80, 24))
    if big:
        out = ["", *banner(tick), ""]
    else:
        out = [bold(cyan("♫ music-tools")) + dim("  ·  ") + bold(title), ""]
    out += header
    if header:
        out.append("")
    column = min(max((_shown_len(label) for label, hint in rows if hint), default=0), HINT_COLUMN_MAX)
    lines = []
    for i, (label, hint) in enumerate(rows):
        num = dim(f"{i + 1:>2}") if i < 9 else "  "
        if i == cursor:
            line = f" {cyan('❯')} {num} {bold(label)}"
        else:
            line = f"   {num} {label}"
        if hint:   # hints line up in one column
            line += " " * (2 + max(0, column - _shown_len(label))) + dim(hint)
        lines.append(line)
    room = size.lines - len(out) - 3   # the blank line, the footer, and one spare so the screen never scrolls
    if len(lines) > room:   # a list taller than the window scrolls with the cursor
        first, last = _window(len(lines), cursor, max(3, room - 2))
        above, below = first, len(lines) - last
        out += [dim(f"      ↑ {above} more") if above else ""]
        out += lines[first:last]
        out += [dim(f"      ↓ {below} more") if below else ""]
    else:
        out += lines
    out += ["", dim(footer)]
    out = [_fit_line(line, size.columns - 1) for line in out]
    if redraw and ANSI:
        # overwrite in place (no flicker): home, each line + clear-to-end, clear below
        sys.stdout.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in out) + "\x1b[J")
        sys.stdout.flush()
    else:
        clear()
        print("\n".join(out))


def menu(title, rows, header=(), start=0, back_label="back", big=False):
    """rows: list of (label, hint). Returns the chosen index, or None to go back."""
    header = list(header)
    if not raw_mode():
        clear()
        print(bold(title))
        for line in header:
            print(line)
        for i, (label, hint) in enumerate(rows, 1):
            print(f"  {i}. {label}" + (f"  ({hint})" if hint else ""))
        ans = input(f"Choose 1-{len(rows)} (Enter to go {back_label}): ").strip()
        return int(ans) - 1 if ans.isdigit() and 1 <= int(ans) <= len(rows) else None
    cursor = min(start, len(rows) - 1)
    footer = f"↑/↓ move · Enter choose · 1-9 jump · q {back_label}"
    animate = big and ANIMATE
    tick, first = 0, True
    while True:
        _render(title, header, rows, cursor, footer, big, tick if animate else None, redraw=not first)
        first = False
        key = read_key(FRAME_SECONDS if animate else None)
        if key is None:
            tick += 1
            continue
        if key in ("up", "k"):
            cursor = (cursor - 1) % len(rows)
        elif key in ("down", "j", "\t"):
            cursor = (cursor + 1) % len(rows)
        elif key in ("enter", "right", "l"):
            return cursor
        elif key.isdigit() and 1 <= int(key) <= min(9, len(rows)):
            return int(key) - 1
        elif key in ("q", "esc", "left", "h"):
            return None


def checklist(title, options, run_label, header=()):
    """
    options: list of [label, checked, hint]. Space/Enter toggles; Enter on the
    last row runs. Returns the list of booleans, or None to go back.
    """
    states = [o[1] for o in options]
    cursor = len(options)  # start on the run button
    while True:
        rows = [(f"{green('[x]') if st else dim('[ ]')} {o[0]}", o[2]) for o, st in zip(options, states)]
        rows.append((green(f"▶ {run_label}"), ""))
        if not raw_mode():
            choice = menu(title + " (pick a number to switch it on/off)", rows, header)
            if choice is None:
                return None
            if choice == len(options):
                return states
            states[choice] = not states[choice]
            continue
        _render(title, list(header), rows, cursor, "↑/↓ move · Space/Enter switch on/off · Enter on ▶ to run · q back")
        key = read_key()
        if key in ("up", "k"):
            cursor = (cursor - 1) % len(rows)
        elif key in ("down", "j", "\t"):
            cursor = (cursor + 1) % len(rows)
        elif key in ("space", "enter", "x"):
            if cursor == len(options):
                if key == "enter":
                    return states
            else:
                states[cursor] = not states[cursor]
        elif key.isdigit() and 1 <= int(key) <= len(options):
            states[int(key) - 1] = not states[int(key) - 1]
            cursor = int(key) - 1
        elif key in ("q", "esc", "left", "h"):
            return None


def _path_completer(text, state):
    matches = glob.glob(os.path.expanduser(text) + "*")
    matches = [m + "/" if os.path.isdir(m) else m for m in matches]
    return matches[state] if state < len(matches) else None


def ask(prompt, default="", paths=False):
    """Read a line. Returns None if the user cancels (Ctrl-C / Ctrl-D)."""
    if readline and paths:
        readline.set_completer_delims("\t\n")
        readline.set_completer(_path_completer)
        readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete")
    suffix = f" {dim('[' + default + ']')}" if default else ""
    show_cursor(True)
    try:
        ans = input(f"{prompt}{suffix}: ")
    except (KeyboardInterrupt, EOFError):
        print()
        return None
    finally:
        show_cursor(False)
        if readline and paths:
            readline.set_completer(None)
    ans = clean_path(ans) if paths else ans.strip()
    return ans or default


def ask_folder(prompt):
    print(dim("Type a path (Tab completes where it can) or drag a folder into this window, then Enter."))
    print(dim("Leave empty to cancel."))
    return ask(prompt, paths=True)


def pause(msg="Press Enter to go back to the menu"):
    try:
        input(dim(f"\n{msg}… "))
    except (KeyboardInterrupt, EOFError):
        print()


def confirm(question, default=False):
    rows = [("Yes", ""), ("No", "")] if default else [("No", ""), ("Yes", "")]
    choice = menu(question, rows, back_label="cancel")
    return choice is not None and rows[choice][0] == "Yes"


def flash(msg):
    print(msg)
    pause("Press Enter to continue")


# ---------------------------------------------------------------- running the tools
def run_tool(name: str, args: list) -> int:
    """
    Run one of the scripts in the foreground. Ctrl-C goes to the script (which
    stops safely); this process ignores it so it can't kill the script mid-write.
    """
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    show_cursor(True)
    # an ignored Ctrl-C is inherited by the child on macOS/Linux, so reset it there (Windows has no preexec_fn)
    extra = {} if IS_WINDOWS else {"preexec_fn": lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)}
    try:
        return subprocess.call([sys.executable, str(HERE / SCRIPTS[name]), *args], **extra)
    finally:
        signal.signal(signal.SIGINT, old)


def run_and_wait(name, args, config):
    clear()
    extra = ["--config", str(config)] if config else []
    print(dim("$ " + " ".join(shell_quote(a) for a in ["mt", name, *args])) + "\n")
    code = run_tool(name, extra + args)
    if code not in (0, None):
        print(red(f"\nFinished with exit code {code}."))
    pause()


# ---------------------------------------------------------------- screens
class App:
    def __init__(self, config=None):
        self.explicit = config
        self.path = config_path(config)

    @property
    def cfg(self):
        return read_raw_config(self.path)

    def save(self, cfg) -> bool:
        try:
            save_config(cfg, self.path)
            return True
        except SystemExit as e:  # validation problem
            flash(red(str(e)))
            return False

    def music_dir(self, cfg=None):
        return str(Path((cfg or self.cfg)["music_dir"]).expanduser())

    def status_lines(self, cfg):
        md = self.music_dir(cfg)
        ok = Path(md).is_dir()
        lines = [f"Music folder  {md}  " + (green("✓") if ok else red("✗ not found (drive unplugged?)"))]
        folders = cfg["album_art"]["folders"]
        for i, f in enumerate(folders):
            full = resolve(f, md)
            mark = green("✓") if full.is_dir() else red("✗ not found")
            lines.append(f"{'Art folders ' if i == 0 else '            '}  {f}  {mark}")
        if not folders:
            lines.append("Art folders   " + yellow("none set"))
        return lines

    # ---- main
    def main(self):
        cursor = 0
        while True:
            cfg = self.cfg
            rows = [
                ("Do it all for me", "looks at your library, shows what it found, asks what to do"),
                ("Tidy my files", "junk, duplicate folders and songs, folder layout, wrong tags"),
                ("Add what's missing", "album art, artist pictures, lyrics"),
                ("Check my library", "health check and lists of what's missing, changes nothing"),
                ("Folders", "change which folders are used"),
                ("Settings", "matching, cropping, tag options…"),
                ("Logs", "see what previous runs did"),
                ("Help", "all commands"),
                ("Quit", ""),
            ]
            choice = menu("Main menu", rows, self.status_lines(cfg), cursor, back_label="quit", big=True)
            if choice is None or choice == len(rows) - 1:
                clear()
                return
            cursor = choice
            [self.run_all, self.tidy, self.add_missing, self.check, self.folders, self.settings,
             self.logs, self.help][choice]()

    def submenu(self, title, build):
        """A screen of choices. `build()` gives [(label, hint, what to do)] and is called again after each choice,
        so a count in a hint (songs to retry) is always current."""
        cursor = 0
        while True:
            entries = build()
            choice = menu(title, [(label, hint) for label, hint, _ in entries], self.status_lines(self.cfg), cursor)
            if choice is None:
                return
            cursor = choice
            entries[choice][2]()

    # ---- the goal screens
    def tidy(self):
        self.submenu("Tidy my files", lambda: [
            ("Clean up junk and duplicate folders", "merge duplicate albums, delete ._ files, empty folders; asks first, can undo",
             self.layout),
            ("Remove duplicate songs", "keep the copy inside an album, remove the loose ones", self.duplicates),
            ("Sort songs into Artist/Album folders", "for Jellyfin, Plex or Navidrome", self.organize),
            ("Fix wrong tags", "rebuild tags from filenames", self.fix_tags),
            ("Undo the last tidy", "puts every moved file back",
             lambda: run_and_wait("layout", ["--undo", "--apply"], self.explicit)),
        ])

    def add_missing(self):
        def build():
            failed = self.failed_count()
            return [
                ("Album art", "search YouTube and add covers", self.add_art),
                ("Artist pictures", "a photo for every artist, from Deezer", self.artists),
                ("Lyrics", "translate foreign songs: original + romaji + English", self.lyrics),
                (f"Retry songs that failed ({failed})" if failed else dim("Retry songs that failed"),
                 "songs that didn't get art last time" if failed else "nothing to retry", self.retry),
            ]
        self.submenu("Add what's missing", build)

    def check(self):
        def listing(tool, flag, title, section):
            def run():
                scope = self.pick_scope(title, section)
                if scope is not None:
                    run_and_wait(tool, [flag, *scope] if flag else scope, self.explicit)
            return run
        self.submenu("Check my library", lambda: [
            ("Health check", "cover art, artist pictures, lyrics and tags at a glance", self.stats),
            ("List songs without art", "changes nothing", self.find_missing),
            ("List artists without a picture", "changes nothing",
             listing("artists", "--list-missing", "Artists without a picture", "artist_art")),
            ("List songs without lyrics", "changes nothing", listing("lyrics", "--list-missing", "Songs without lyrics", "lyrics")),
            ("List duplicate songs", "changes nothing", listing("duplicates", None, "Duplicate songs", "duplicates")),
        ])

    def failed_count(self):
        try:
            return len([l for l in FAILED_LIST.read_text(encoding="utf-8").splitlines() if l.strip()])
        except OSError:
            return 0

    # ---- album art
    def pick_scope(self, title, section="album_art"):
        """Returns a list of paths ([] = the configured folders), or None to go back."""
        cfg = self.cfg
        folders = cfg[section]["folders"]
        shown = "whole music folder" if folders == ["."] else ", ".join(folders) or "none set"
        label = {"album_art": "Art folders", "artist_art": "Artist folders", "lyrics": "Lyrics folders",
                 "duplicates": "Duplicate-check folders", "organize": "Organize folders"}[section]
        rows = [(label, shown), ("A different folder or song…", "just this once")]
        choice = menu(title + " · where?", rows, self.status_lines(cfg))
        if choice is None:
            return None
        if choice == 0:
            return []
        clear()
        p = ask_folder("Folder or song")
        if not p:
            return None
        if not Path(p).expanduser().exists():
            flash(red(f"Not found: {p}"))
            return None
        return [p]

    def find_missing(self):
        scope = self.pick_scope("Find songs without art")
        if scope is not None:
            run_and_wait("art", ["--list-missing", *scope], self.explicit)

    def art_options(self, title, run_label, include_force=True):
        a = self.cfg["album_art"]
        opts = [
            ["Ask me when a match isn't exact", True, "off = skip those songs"],
            ["Convert .webm files so they can hold art", a["convert_webm"], "lossless, original goes to Trash"],
            ["Crop covers to a square", a["crop"], "off = keep the wide YouTube thumbnail"],
            ["Preview only (don't write anything)", False, ""],
        ]
        if include_force:
            opts.append(["Replace art songs already have", False, "redo everything in scope"])
        states = checklist(title, opts, run_label)
        if states is None:
            return None
        flags = []
        if not states[0]:
            flags.append("--auto")
        if states[1]:
            flags.append("--convert-webm")
        if not states[2]:
            flags.append("--no-crop")
        if states[3]:
            flags.append("--dry-run")
        if include_force and states[4]:
            flags.append("--force")
        return flags

    def add_art(self):
        scope = self.pick_scope("Add missing art")
        if scope is None:
            return
        flags = self.art_options("Add missing art · options", "Start")
        if flags is not None:
            run_and_wait("art", [*scope, *flags], self.explicit)

    def retry(self):
        if not self.failed_count():
            flash("Nothing to retry — every song from the last run got its art.")
            return
        clear()
        print(bold(f"{self.failed_count()} song(s) from last time:"))
        for line in FAILED_LIST.read_text(encoding="utf-8").splitlines()[:20]:
            print("  " + Path(line).name)
        pause("Press Enter to choose options")
        flags = self.art_options("Retry failed songs · options", "Retry", include_force=False)
        if flags is not None:
            run_and_wait("art", ["--retry", *flags], self.explicit)

    # ---- artist pictures
    def artists(self):
        scope = self.pick_scope("Find artist pictures", "artist_art")
        if scope is None:
            return
        states = checklist("Find artist pictures · options", [
            ["List artists without a picture only", False, "no downloading, changes nothing"],
            ["Ask me when a name isn't an exact match", True, "off = skip those artists"],
            ["Preview only (don't save anything)", False, ""],
            ["Replace pictures that already exist", False, ""],
        ], "Start")
        if states is None:
            return
        flags = []
        if states[0]:
            flags.append("--list-missing")
        if not states[1]:
            flags.append("--auto")
        if states[2]:
            flags.append("--dry-run")
        if states[3]:
            flags.append("--force")
        run_and_wait("artists", [*scope, *flags], self.explicit)

    # ---- lyrics
    def lyrics(self):
        scope = self.pick_scope("Find lyrics", "lyrics")
        if scope is None:
            return
        cfg_layers = self.cfg["lyrics"]["layers"]
        while True:
            states = checklist("Find lyrics · options", [
                ["List songs without lyrics only", False, "no fetching, changes nothing"],
                ["Original writing (kanji / hangul)", "original" in cfg_layers, "off = romanization + English only"],
                ["Romanization (romaji / pinyin)", "romanization" in cfg_layers, "off = original + English only"],
                ["English translation", "english" in cfg_layers, "off = original + romanization only"],
                ["Also save songs that are already English", False, "usually off: only foreign songs need it"],
                ["Translate lyrics that are already romanized", True, "romaji .lrc files get English added (Korean needs Claude)"],
                ["Switch layers of finished songs", False, "re-applies the choices above, no lookups"],
                ["Put your original .lrc back", False, "undoes a translation added earlier (no lookups)"],
                ["Preview only (don't save anything)", False, ""],
                ["Replace lyrics files that already exist", False, "your own .lrc files are kept as .bak"],
            ], "Start")
            if states is None:
                return
            if any(states[1:4]):
                break
            flash(red("Pick at least one of original, romanization and English."))
        flags = []
        if states[0]:
            flags.append("--list-missing")
        if not all(states[1:4]):
            names = [n for n, on in zip(("original", "romanization", "english"), states[1:4]) if on]
            flags += ["--layers", ",".join(names)]
        if not states[3]:
            flags.append("--no-translate")
        if states[4]:
            flags.append("--include-english")
        if not states[5]:
            flags.append("--no-romanized")
        if states[6]:
            flags.append("--relayer")
        if states[7]:
            flags.append("--restore-lrc")
        if states[8]:
            flags.append("--dry-run")
        if states[9]:
            flags.append("--force")
        run_and_wait("lyrics", [*scope, *flags], self.explicit)

    # ---- tags
    def fix_tags(self):
        cfg = self.cfg
        rows = [("Whole music folder", self.music_dir(cfg)),
                ("A different folder or song…", ""),
                ("Only paths containing some text…", "e.g. an artist name")]
        choice = menu("Fix wrong tags · where?", rows, self.status_lines(cfg))
        if choice is None:
            return
        scope = []
        if choice == 1:
            clear()
            p = ask_folder("Folder or song")
            if not p:
                return
            scope = [p]
        elif choice == 2:
            clear()
            text = ask("Only files whose path contains")
            if not text:
                return
            scope = ["--filter", text]
        states = checklist("Fix wrong tags · options", [
            ["Only songs whose tags are clearly wrong", True, "safest; skips spelling/transliteration differences"],
            ["Only list songs that would change", True, ""],
            ["Write the changes", False, "off = preview only"],
        ], "Start")
        if states is None:
            return
        args = [*scope]
        if states[0]:
            args.append("--only-severe")
        if states[1]:
            args.append("--only-mismatched")
        if states[2]:
            if not confirm("Really rewrite the tags of the matching songs?"):
                return
            args.append("--apply")
        run_and_wait("tags", args, self.explicit)

    # ---- duplicates
    def duplicates(self):
        scope = self.pick_scope("Duplicate songs", "duplicates")
        if scope is None:
            return
        ticked = [False, False, False]
        while True:
            ticked = checklist("Duplicate songs · options", [
                ["Remove the loose copies", ticked[0], "keeps the copy inside an album; off = just list the duplicates"],
                ["Preview only (dry run)", ticked[1], "show which copies would go, change nothing"],
                ["Apply the changes to all", ticked[2], "really remove them (asks once more first)"],
            ], "Start")
            if ticked is None:
                return
            remove, preview, apply_now = ticked
            if apply_now and preview:
                flash(red("Pick either Preview (changes nothing) or Apply (removes them), not both."))
            elif apply_now and not remove:
                flash(red("Apply needs 'Remove the loose copies' ticked: the plain duplicate list never changes anything."))
            else:
                break
        args = [*scope]
        if remove:
            args.append("--delete-strays")
        if preview:
            args.append("--dry-run")
        if apply_now:
            args.append("--apply")
        run_and_wait("duplicates", args, self.explicit)

    # ---- organize
    def organize(self):
        scope = self.pick_scope("Organize library into Artist/Album folders", "organize")
        if scope is None:
            return
        opts = [
            ["Auto-detect albums for loose songs", True, "finds official album instead of Singles"],
            ["Copy artist picture to folder.jpg", True, "for Jellyfin / Plex support"],
            ["Clean empty folders after moving", True, "removes old empty source folders"],
            ["Preview only (dry run)", False, "show what would move without moving anything"],
        ]
        chosen = checklist("Organize into Artist/Album folders", opts, "Organize songs", self.status_lines(self.cfg))
        if chosen is None:
            return
        auto_album, copy_art, clean, dry_run = chosen
        args = [*scope]
        if dry_run:
            args.append("--dry-run")
        if not auto_album:
            args.append("--no-auto-album")
        if not copy_art:
            args.append("--no-artist-art")
        if not clean:
            args.append("--no-clean")
        run_and_wait("organize", args, self.explicit)

    # ---- all-in-one run
    def run_all(self):
        """The tool looks at the library and asks what to do (steps, preview, apply) when it runs in a terminal."""
        run_and_wait("all", [], self.explicit)

    # ---- library stats
    def stats(self):
        run_and_wait("stats", [], self.explicit)

    # ---- folder layout
    LAYOUT_ACTIONS = (
        ("Show the report", "duplicate albums, junk, odd names — changes nothing", []),
        ("Tidy everything", "delete junk, merge ALL duplicate albums and artist folders; shows the list, asks, saves an undo file",
         ["--clean", "--merge-artists", "--merge-albums", "--apply"]),
        ("Merge artist folders spelled two ways", "100 gecs / 100gecs: all their files move into the fuller folder",
         ["--merge-artists", "--apply"]),
        ("Merge all duplicate albums", "songs and lyrics move into the fuller folder; nothing is overwritten",
         ["--merge-albums", "--apply"]),
        ("Clean junk only", "._ files, .DS_Store, .lrc.bak files; empty folders", ["--clean", "--apply"]),
        ("Undo the last tidy", "puts every moved file back", ["--undo", "--apply"]),
    )

    def layout(self):
        """Report, or tidy: the tool itself prints what it would do and asks before changing anything.
        Collaboration folders and odd names (Unknown, null) are only ever reported."""
        cursor = 0
        while True:
            rows = [(label, hint) for label, hint, _ in self.LAYOUT_ACTIONS]
            choice = menu("Check and tidy folders", rows, self.status_lines(self.cfg), cursor)
            if choice is None:
                return
            cursor = choice
            run_and_wait("layout", list(self.LAYOUT_ACTIONS[choice][2]), self.explicit)

    # ---- folders
    FOLDER_SECTIONS = (("album_art", "Album art folders"), ("artist_art", "Artist picture folders"),
                       ("lyrics", "Lyrics folders"), ("duplicates", "Duplicate-check folders"))

    def folders(self):
        cursor = 0
        while True:
            cfg = self.cfg
            rows = [("Change music folder", self.music_dir(cfg))]
            for section, label in self.FOLDER_SECTIONS:
                folders = cfg[section]["folders"]
                shown = "whole music folder" if folders == ["."] else ", ".join(folders) or "none set"
                rows.append((label, shown))
            rows.append(("Back", ""))
            choice = menu("Folders", rows, self.status_lines(cfg), cursor)
            if choice is None or choice == len(rows) - 1:
                return
            cursor = choice
            if choice == 0:
                clear()
                p = ask_folder("New music folder")
                if p:
                    new = Path(p).expanduser()
                    if not new.is_dir() and not confirm(f"{new} doesn't exist. Save it anyway?"):
                        continue
                    cfg["music_dir"] = str(new.resolve() if new.is_dir() else new)
                    self.save(cfg)
            else:
                section, label = self.FOLDER_SECTIONS[choice - 1]
                self.edit_folders(section, label)

    def edit_folders(self, section, label):
        """Add/remove/replace the folder list for one section (album_art, artist_art or lyrics)."""
        cursor = 0
        while True:
            cfg = self.cfg
            folders = cfg[section]["folders"]
            shown = "whole music folder" if folders == ["."] else ", ".join(folders) or "none set"
            rows = [("Add a folder", ""), ("Remove a folder", ""), ("Use only one folder…", "replaces the list"),
                    ("Back", "")]
            choice = menu(label, rows, [f"Now:  {shown}"], cursor, back_label="back")
            if choice is None or choice == 3:
                return
            cursor = choice
            if choice in (0, 2):
                clear()
                print(dim("Paths inside the music folder can be relative, e.g. \"Mixes\".\n"))
                p = ask_folder("Folder")
                if not p:
                    continue
                entry = self.folder_entry(p, cfg)
                if entry is None:
                    continue
                if choice == 2:
                    cfg[section]["folders"] = [entry]
                elif entry not in folders:
                    folders.append(entry)
                self.save(cfg)
            elif choice == 1:
                if not folders:
                    flash(f"There are no {label.lower()} to remove.")
                    continue
                pick = menu("Remove which folder?", [(f, "") for f in folders])
                if pick is not None:
                    folders.pop(pick)
                    self.save(cfg)

    def folder_entry(self, text, cfg):
        p = Path(text).expanduser()
        if p.is_dir():
            return str(p.resolve())
        inside = resolve(text, self.music_dir(cfg))
        if not p.is_absolute() and inside.is_dir():
            return text
        if confirm(f"{text} doesn't exist. Add it anyway?"):
            return text
        return None

    # ---- settings
    SETTINGS = [
        ("album_art.min_match", "Match strictness", "1.0 = YouTube title must match exactly; lower accepts looser matches"),
        ("album_art.convert_webm", "Convert .webm files", "turn .webm into .opus so they can hold art"),
        ("album_art.crop", "Crop covers to square", "off keeps the wide 16:9 thumbnail"),
        ("album_art.search_results", "Results to show when asking", "how many YouTube results to choose from"),
        ("album_art.sidecar_images", "Use images next to songs", "song.jpg next to song.flac is used first"),
        ("album_art.force", "Always replace existing art", "usually off"),
        ("album_art.jpeg_quality", "Cover image quality", "1-100"),
        ("artist_art.output_dir", "Artist pictures folder", "where <Artist>.jpg files go (relative to the music folder)"),
        ("artist_art.placement", "Where artist pictures go", "shared (one folder) · artist_folder (artist.jpg in each artist's folder)"),
        ("artist_art.search_results", "Artists to show when asking", "how many Deezer artists to choose from"),
        ("artist_art.force", "Always replace artist pictures", "usually off"),
        ("lyrics.only_foreign", "Skip English songs", "lyrics: only songs that aren't in English are translated and saved"),
        ("lyrics.translate_romanized", "Translate romanized lyrics", "lyrics already in romaji get an English translation too"),
        ("lyrics.claude_model", "Claude model for lyrics", "used only if claude is in lyrics.translators"),
        ("lyrics.force", "Always replace lyrics files", "usually off"),
        ("tags.title_mode", "Title from filename", "auto · stem (whole filename) · parsed"),
        ("tags.album_mode", "Album tag", "folder name · clear · same as title · keep"),
        ("tags.update_artist", "Set artist from filename", "for names like 'Artist - Title'"),
        ("tags.clear_picard", "Remove MusicBrainz IDs", "IDs that link songs to the wrong album"),
        ("tags.clear_tracks", "Clear wrong track numbers", "numbers in the filename are kept"),
        ("duplicates.tolerance_seconds", "Duplicate match tolerance", "seconds two lengths can differ by and still count as the same recording"),
        ("workers", "Songs at once", "higher is faster; 1-64"),
        ("save_logs", "Save logs", "keep a copy of each run in the logs folder"),
    ]
    CHOICE_HELP = {
        "auto": "clean title; full filename for live sets",
        "stem": "always the whole filename",
        "parsed": "only the part after 'Artist - '",
        "folder": "the folder the song is in",
        "clear": "remove the album tag",
        "title": "same as the song title",
        "keep": "leave it alone",
    }

    @staticmethod
    def _get(cfg, key):
        section, _, name = key.rpartition(".")
        return (cfg[section] if section else cfg)[name]

    @staticmethod
    def _set(cfg, key, value):
        section, _, name = key.rpartition(".")
        (cfg[section] if section else cfg)[name] = value

    @staticmethod
    def _show(value):
        if isinstance(value, bool):
            return green("on") if value else dim("off")
        return str(value)

    def settings(self):
        cursor = 0
        while True:
            cfg = self.cfg
            rows = [(f"{label}: {self._show(self._get(cfg, key))}", hint) for key, label, hint in self.SETTINGS]
            rows.append(("Reset everything to defaults", ""))
            rows.append(("Open the config file in a text editor", str(self.path)))
            choice = menu("Settings", rows, start=cursor)
            if choice is None:
                return
            cursor = choice
            if choice == len(self.SETTINGS):
                if confirm("Reset all settings (except folders) to defaults?"):
                    fresh = copy.deepcopy(DEFAULTS)
                    fresh["music_dir"] = cfg["music_dir"]
                    fresh["album_art"]["folders"] = cfg["album_art"]["folders"]
                    fresh["artist_art"]["folders"] = cfg["artist_art"]["folders"]
                    fresh["lyrics"]["folders"] = cfg["lyrics"]["folders"]
                    fresh["duplicates"]["folders"] = cfg["duplicates"]["folders"]
                    self.save(fresh)
                continue
            if choice == len(self.SETTINGS) + 1:
                if not self.path.is_file():
                    self.save(cfg)
                if not open_path(self.path, edit=True):
                    flash(f"No program found to open it with. The settings file is {self.path}")
                continue
            key, label, hint = self.SETTINGS[choice]
            current = self._get(cfg, key)
            section, _, name = key.rpartition(".")
            if isinstance(current, bool):
                self._set(cfg, key, not current)
            elif (section, name) in CHOICES:
                allowed = CHOICES[(section, name)]
                pick = menu(label, [(c, self.CHOICE_HELP.get(c, "")) for c in allowed],
                            start=allowed.index(current) if current in allowed else 0)
                if pick is None:
                    continue
                self._set(cfg, key, allowed[pick])
            else:
                clear()
                print(bold(label) + "\n" + dim(hint) + "\n")
                text = ask("New value", str(current))
                if text is None:
                    continue
                if isinstance(current, str):
                    if not text:
                        continue
                    self._set(cfg, key, text)
                    self.save(cfg)
                    continue
                try:
                    value = float(text) if isinstance(current, float) or "." in text else int(text)
                except ValueError:
                    flash(red(f"'{text}' isn't a number."))
                    continue
                self._set(cfg, key, value)
            self.save(cfg)

    # ---- logs
    def logs(self):
        while True:
            logs = sorted(LOG_DIR.glob("*.log"), reverse=True) if LOG_DIR.is_dir() else []
            rows = [(l.stem.replace("_", "  ", 1), "") for l in logs[:15]]
            rows.append(("Open the logs folder", str(LOG_DIR)))
            header = [] if logs else [dim("No logs yet — they're saved each time a tool runs.")]
            choice = menu("Logs (newest first)", rows, header)
            if choice is None:
                return
            if choice == len(rows) - 1:
                LOG_DIR.mkdir(exist_ok=True)
                if not open_path(LOG_DIR):
                    flash(f"No program found to open it with. The logs are in {LOG_DIR}")
                continue
            clear()
            self.show_log(logs[choice])

    @staticmethod
    def show_log(path):
        pager = shutil.which("less")
        if sys.stdout.isatty() and pager:
            subprocess.call([pager, "-R", "+G", str(path)])
            return
        print(Path(path).read_text(encoding="utf-8", errors="replace"))
        if sys.stdout.isatty():  # no pager (Windows): keep the text on screen until asked
            pause()

    def help(self):
        clear()
        print(self.help_text)
        pause()


def run(config=None, help_text=""):
    if ANSI:
        sys.stdout.write("\x1b[?25l")  # hide the cursor in menus
    try:
        app = App(config)
        app.help_text = help_text
        app.main()
    except KeyboardInterrupt:
        clear()
    finally:
        if ANSI:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()
    return 0
