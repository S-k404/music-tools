"""Shared helpers: config loading, file discovery, progress bars, Ctrl-C handling."""

import atexit
import copy
import importlib.util
import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11 or newer is required (you have {sys.version.split()[0]}).")

import tomllib


def require(*modules: str) -> None:
    """Exit with install instructions if a required package is missing."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        names = {"PIL": "Pillow"}
        pkgs = " ".join(names.get(m, m) for m in missing)
        sys.exit(f"Missing Python package(s): {pkgs}\n"
                 f"Run ./setup.sh once, or: python3 -m pip install {pkgs}")

try:
    from tqdm import tqdm
except ImportError:  # progress bars are optional
    tqdm = None

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "music_dir": "~/Music",
    "workers": 8,
    "save_logs": True,
    "album_art": {
        "folders": ["."],
        "min_match": 1.0,
        "search_results": 5,
        "crop": True,
        "force": False,
        "jpeg_quality": 92,
        "sidecar_images": True,
        "convert_webm": False,
    },
    "artist_art": {
        "folders": ["."],
        "output_dir": "Artist Art",
        "search_results": 5,
        "force": False,
    },
    "lyrics": {
        "folders": ["."],
        "formats": ["lrc", "html"],
        "translators": ["google", "mymemory", "deepl", "claude"],
        "claude_model": "claude-sonnet-5",
        "only_foreign": True,
        "translate_romanized": True,
        "layers": ["original", "romanization", "english"],
        "force": False,
        "backup_dir": "",
    },
    "tags": {
        "title_mode": "auto",
        "album_mode": "folder",
        "update_artist": True,
        "clear_picard": True,
        "clear_tracks": True,
    },
    "duplicates": {
        "folders": ["."],
        "tolerance_seconds": 3.0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


CHOICES = {
    ("tags", "title_mode"): ("auto", "stem", "parsed"),
    ("tags", "album_mode"): ("folder", "clear", "title", "keep"),
}


def _type_ok(val, default) -> bool:
    if isinstance(default, bool):
        return isinstance(val, bool)
    if isinstance(default, (int, float)):
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return isinstance(val, type(default))


def _validate(cfg: dict, source: str) -> None:
    """Exit with a clear message if a config value has the wrong type or range."""
    problems = []
    for key, default in DEFAULTS.items():
        if isinstance(default, dict):
            if not isinstance(cfg.get(key), dict):
                problems.append(f"[{key}] should be a section")
                continue
            for sub, sub_default in default.items():
                if not _type_ok(cfg[key][sub], sub_default):
                    problems.append(f"{key}.{sub} = {cfg[key][sub]!r} has the wrong type (example: {sub_default!r})")
        elif not _type_ok(cfg[key], default):
            problems.append(f"{key} = {cfg[key]!r} has the wrong type (example: {default!r})")
    if not problems:
        for (section, key), allowed in CHOICES.items():
            if cfg[section][key] not in allowed:
                problems.append(f"{section}.{key} must be one of: {', '.join(allowed)} (got {cfg[section][key]!r})")
        art = cfg["album_art"]
        if not all(isinstance(f, str) for f in art["folders"]):
            problems.append("album_art.folders should be a list of folder paths")
        if not 0 <= art["min_match"] <= 1:
            problems.append("album_art.min_match must be between 0 and 1")
        if not 1 <= art["search_results"] <= 20:
            problems.append("album_art.search_results must be between 1 and 20")
        if not 1 <= art["jpeg_quality"] <= 100:
            problems.append("album_art.jpeg_quality must be between 1 and 100")
        artists = cfg["artist_art"]
        if not all(isinstance(f, str) for f in artists["folders"]):
            problems.append("artist_art.folders should be a list of folder paths")
        if not artists["output_dir"].strip():
            problems.append("artist_art.output_dir can't be empty")
        if not 1 <= artists["search_results"] <= 20:
            problems.append("artist_art.search_results must be between 1 and 20")
        lyrics = cfg["lyrics"]
        if not all(isinstance(f, str) for f in lyrics["folders"]):
            problems.append("lyrics.folders should be a list of folder paths")
        if not lyrics["formats"] or not set(lyrics["formats"]) <= {"lrc", "txt", "html"}:
            problems.append("lyrics.formats should be a list made of: lrc, txt, html")
        if not lyrics["layers"] or not all(isinstance(x, str) for x in lyrics["layers"]):
            problems.append("lyrics.layers should be a list made of: original, romanization, english")
        if not set(lyrics["translators"]) <= {"google", "mymemory", "deepl", "claude"}:
            problems.append("lyrics.translators should be a list made of: google, mymemory, deepl, claude")
        if not isinstance(lyrics["backup_dir"], str):
            problems.append("lyrics.backup_dir should be a folder path (or empty)")
        dups = cfg["duplicates"]
        if not all(isinstance(f, str) for f in dups["folders"]):
            problems.append("duplicates.folders should be a list of folder paths")
        if not 0 <= dups["tolerance_seconds"] <= 30:
            problems.append("duplicates.tolerance_seconds must be between 0 and 30")
        if not 1 <= cfg["workers"] <= 64:
            problems.append("workers must be between 1 and 64")
    if problems:
        sys.exit(f"Problem in {source}:\n  " + "\n  ".join(problems))


def config_candidates(explicit=None):
    if explicit:
        yield Path(explicit).expanduser()
    if os.environ.get("MUSIC_TOOLS_CONFIG"):
        yield Path(os.environ["MUSIC_TOOLS_CONFIG"]).expanduser()
    yield HERE / "config.toml"
    yield Path("~/.config/music-tools/config.toml").expanduser()


def load_config(explicit=None) -> dict:
    """Defaults, overridden by the config file (see config_path), then by MUSIC_DIR."""
    cfg = DEFAULTS
    path = config_path(explicit)
    if path.is_file():
        cfg = read_raw_config(path)
        cfg["_path"] = str(path)
    elif explicit:
        sys.exit(f"Config file not found: {path}")
    if os.environ.get("MUSIC_DIR"):
        cfg = _merge(cfg, {"music_dir": os.environ["MUSIC_DIR"]})
    _validate(cfg, cfg.get("_path", "defaults"))
    cfg["music_dir"] = str(Path(cfg["music_dir"]).expanduser())
    return cfg


def config_path(explicit=None) -> Path:
    """
    The config file settings are read from and written to. An explicit
    --config or $MUSIC_TOOLS_CONFIG is always used as-is, even if it doesn't
    exist yet, so a new config is never written over a different one.
    """
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("MUSIC_TOOLS_CONFIG"):
        return Path(os.environ["MUSIC_TOOLS_CONFIG"]).expanduser()
    for path in config_candidates():
        if path.is_file():
            return path
    return HERE / "config.toml"


def read_raw_config(path: Path) -> dict:
    """The config as stored (defaults filled in, nothing expanded)."""
    if not path.is_file():
        return copy.deepcopy(DEFAULTS)
    try:
        with open(path, "rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    except (tomllib.TOMLDecodeError, OSError) as e:
        sys.exit(f"Could not read config {path}: {e}")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON strings are valid TOML strings


def save_config(cfg: dict, path: Path) -> None:
    """Validate, then write the config atomically (never leaves a half-written file)."""
    _validate(cfg, "the new settings")
    lines = ["# music-tools settings. Every option is explained in config.example.toml.", ""]
    lines += [f"{k} = {_toml_value(v)}" for k, v in cfg.items() if not isinstance(v, dict) and not k.startswith("_")]
    for section, values in cfg.items():
        if isinstance(values, dict):
            lines += ["", f"[{section}]"] + [f"{k} = {_toml_value(v)}" for k, v in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def resolve(folder: str, music_dir: str) -> Path:
    """Folders in the config may be absolute or relative to music_dir."""
    p = Path(folder).expanduser()
    return p if p.is_absolute() else Path(music_dir) / p


def folder_problem(path) -> str:
    """
    Why a folder can't be used, in plain words ('' if it can be read). Actually
    lists it, because on macOS an external drive can be mounted and still be
    blocked by the privacy setting for removable volumes, which looks like
    "not found" or "empty" everywhere else.
    """
    p = Path(path).expanduser()
    try:
        os.listdir(p)
        return ""
    except PermissionError:
        return (f"macOS is blocking access to {p}. Allow your terminal app (or the app you're running this from) "
                "under System Settings > Privacy & Security > Files & Folders > Removable Volumes "
                "(or Full Disk Access), then run it again.")
    except NotADirectoryError:
        return f"{p} is a file, not a folder"
    except FileNotFoundError:
        parts = p.parts
        if len(parts) > 2 and parts[1] == "Volumes" and not Path(*parts[:3]).exists():
            return f"the drive {parts[2]!r} isn't connected (or isn't mounted)"
        return f"{p} doesn't exist"
    except OSError as e:
        return f"can't read {p}: {e.strerror or e}"


_hinted = set()


def _walk_error(e: OSError) -> None:
    """os.walk hit a folder it can't read: say so, and (once per drive) say what to do about it."""
    log(f"! can't read {e.filename}: {e.strerror}")
    parts = Path(str(e.filename)).parts
    drive = str(Path(*parts[:3])) if len(parts) > 2 else ""
    if isinstance(e, PermissionError) and len(parts) > 2 and parts[1] == "Volumes" and drive not in _hinted:
        _hinted.add(drive)
        log("  " + folder_problem(drive))


def find_audio(paths, exts, keyword=None):
    """Yield audio files under the given files/folders, sorted, skipping macOS ._ files."""
    for base in (Path(p).expanduser() for p in paths):
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(
                Path(root) / f
                for root, _, files in os.walk(base, onerror=_walk_error)
                for f in files
            )
        else:
            why = folder_problem(base)
            log(f"Skipping {base}: {why or 'not found'}")
            continue
        for p in candidates:
            if p.name.startswith(".") or p.suffix.lower() not in exts:
                continue
            if keyword and keyword.lower() not in str(p).lower():
                continue
            yield p


def progress(iterable, total=None, desc="", disable=False, unit="file"):
    if tqdm is None or disable:
        return iterable
    return tqdm(iterable, total=total, desc=desc, unit=unit, dynamic_ncols=True, leave=False, colour="#5fd7ff",
                bar_format="  {desc:<17}{bar:28} {percentage:3.0f}%  {n_fmt}/{total_fmt} {unit}s  {elapsed}<{remaining}")


def log(msg: str = ""):
    """Print without breaking an active progress bar."""
    if tqdm is not None:
        tqdm.write(msg)
    else:
        print(msg)


# ---------------------------------------------------------------- styling
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code):
    return (lambda s: f"\x1b[{code}m{s}\x1b[0m") if COLOR else (lambda s: s)


bold, dim, cyan, green, red, yellow, magenta = _c("1"), _c("2"), _c("36"), _c("32"), _c("31"), _c("33"), _c("35")
pink, violet, sky = _c("38;5;205"), _c("38;5;135"), _c("38;5;81")


def text_width(s: str) -> int:
    """Columns a string takes on screen (CJK is double width, accents take none)."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def fit(s: str, width: int) -> str:
    """Cut with an ellipsis or pad with spaces so the text is exactly `width` columns."""
    if text_width(s) > width:
        while s and text_width(s) > width - 1:
            s = s[:-1]
        s += "…"
    return s + " " * (width - text_width(s))


def heading(title: str, subtitle: str = "") -> None:
    """A small coloured title block at the top of a tool's output."""
    print(f"\n  {pink('♫')} {bold(title)}" + (f"  {dim(subtitle)}" if subtitle else ""))
    print("  " + dim("─" * 46))


def section(title: str) -> None:
    print(f"\n  {bold(title)}")


# ---------------------------------------------------------------- Ctrl-C handling
# The first Ctrl-C asks the run to stop: files already being written are
# finished (never left half-written) and no new ones are started.
STOP = threading.Event()


def _on_ctrl_c(signum, frame):
    if STOP.is_set():
        log("Still finishing the files being written, one moment...")
        return
    STOP.set()
    log("\nStopping: finishing the files in progress, then exiting...")


def install_stop_handler() -> None:
    signal.signal(signal.SIGINT, _on_ctrl_c)


@contextmanager
def normal_ctrl_c():
    """Plain Ctrl-C behaviour (e.g. while waiting for typed input)."""
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)


# ---------------------------------------------------------------- run logs
LOG_DIR = HERE / "logs"
KEEP_LOGS = 200
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Tee:
    """Send output to the terminal and a log file (without colour codes)."""

    def __init__(self, stream, file):
        self.stream, self.file, self.lock = stream, file, threading.Lock()

    def write(self, text):
        with self.lock:
            self.stream.write(text)
            try:
                self.file.write(_ANSI_RE.sub("", text))
            except (OSError, ValueError):
                pass  # a full disk or closed log must never break the run
        return len(text)

    def flush(self):
        self.stream.flush()
        try:
            self.file.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


def start_log(tool: str, cfg: dict):
    """Copy everything this run prints into logs/<date>_<tool>.log. Returns the path."""
    if not cfg.get("save_logs", True):
        return None
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{tool}.log"
        f = open(path, "w", encoding="utf-8")
        f.write(f"# {tool} {' '.join(sys.argv[1:])}\n# {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    except OSError as e:
        log(f"! could not create a log file: {e}")
        return None
    original = sys.stdout
    sys.stdout = _Tee(original, f)

    def finish():
        sys.stdout.flush()
        sys.stdout = original
        f.close()
        print(f"Log saved: {path}")

    atexit.register(finish)
    for old in sorted(LOG_DIR.glob("*.log"))[:-KEEP_LOGS]:
        old.unlink(missing_ok=True)
    return path
