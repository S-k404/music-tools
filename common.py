"""Shared helpers: config loading, file discovery, progress bars, Ctrl-C handling."""

import atexit
import copy
import errno
import functools
import http.client
import importlib.util
import io
import ssl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
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
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

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
        "placement": "shared",   # "shared": every picture in output_dir; "artist_folder": <Artist>/artist.jpg where that folder exists
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
    "layout": {
        "ignore": [],   # top-level folders `mt layout` leaves out (it already skips the artist-picture and lyrics-backup folders)
    },
    "organize": {
        "folders": ["."],
        "fallback_artist": "Unknown Artist",
        "fallback_album": "Singles",
        "auto_album": True,
        "copy_artist_art": True,
        "clean_empty_dirs": True,
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
    ("artist_art", "placement"): ("shared", "artist_folder"),
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
        if not all(isinstance(f, str) for f in cfg["layout"]["ignore"]):
            problems.append("layout.ignore should be a list of folder names")
        org = cfg.get("organize")
        if org:
            if not all(isinstance(f, str) for f in org["folders"]):
                problems.append("organize.folders should be a list of folder paths")
            if not isinstance(org["fallback_artist"], str) or not org["fallback_artist"].strip():
                problems.append("organize.fallback_artist must be a non-empty name")
            if not isinstance(org["fallback_album"], str):
                problems.append("organize.fallback_album must be a folder name (or empty)")
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
    if IS_WINDOWS and os.environ.get("APPDATA"):
        yield Path(os.environ["APPDATA"]) / "music-tools" / "config.toml"
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
    atomic_write(path, "\n".join(lines) + "\n")


def atomic_write(path: Path, data) -> None:
    """Write str/bytes to `path` through a hidden temp file, so a crash never leaves half a file.
    The temp file is removed if the write fails; the error (OSError) is left for the caller."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- web requests
# One kept-alive connection per thread and host, so a run of requests to the same service (lrclib,
# Deezer, Google Translate...) pays for the TLS handshake once per worker instead of once per request.
# Failures raise urllib.error.HTTPError / URLError, exactly as urllib.request.urlopen does, so callers
# keep their existing error handling.
_connections = threading.local()
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest, http.client.ImproperConnectionState,
          BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError)
_REDIRECTS = (301, 302, 303, 307, 308)


@functools.lru_cache(maxsize=None)
def _via_proxy(scheme: str, host: str) -> bool:
    """urllib honours HTTP(S)_PROXY and the system proxy settings; http.client doesn't, so those go through urllib."""
    try:
        return bool(urllib.request.getproxies().get(scheme)) and not urllib.request.proxy_bypass(host)
    except Exception:
        return True


def _drop_connection(key) -> None:
    conn = getattr(_connections, "pool", {}).pop(key, None)
    if conn is not None:
        conn.close()


def fetch_url(url: str, data=None, headers=None, timeout: float = 20, max_bytes: int = None, _hops: int = 0) -> bytes:
    """GET (POST when `data` is given) and return the body. With `max_bytes`, at most max_bytes + 1 bytes are
    read, so the caller can tell the body was bigger without downloading all of it."""
    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme, parts.hostname or ""
    if scheme not in ("http", "https") or _via_proxy(scheme, host):
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=timeout) as r:
            return r.read() if max_bytes is None else r.read(max_bytes + 1)
    port = parts.port or (443 if scheme == "https" else 80)
    key = (scheme, host, port)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    pool = _connections.__dict__.setdefault("pool", {})
    for attempt in (0, 1):
        conn = pool.get(key)
        reused = conn is not None
        if conn is None:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = pool[key] = cls(host, port, timeout=timeout)
        conn.timeout = timeout
        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("POST" if data is not None else "GET", target, body=data, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
            if max_bytes is not None and len(body) > max_bytes:
                _drop_connection(key)  # the rest of the body is still on the wire
        except (OSError, http.client.HTTPException) as e:
            _drop_connection(key)
            stale = isinstance(e, _STALE) or getattr(e, "errno", None) == errno.EBADF
            if stale and reused and attempt == 0:
                continue  # the server closed an idle connection; one fresh try
            raise urllib.error.URLError(e) from e
        break
    if resp.status in _REDIRECTS and _hops < 5 and resp.getheader("Location"):
        same = resp.status in (307, 308)
        return fetch_url(urllib.parse.urljoin(url, resp.getheader("Location")), data if same else None, headers,
                         timeout, max_bytes, _hops + 1)
    if resp.status >= 400:
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(body))
    return body


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------- the system around the tools
_INSTALL_HINTS = {  # program -> (macOS, Windows, anything else)
    "yt-dlp": ("brew install yt-dlp", "winget install yt-dlp.yt-dlp", "pip install yt-dlp"),
    "ffmpeg": ("brew install ffmpeg", "winget install Gyan.FFmpeg", "sudo apt install ffmpeg, or your package manager's"),
}


def install_hint(program: str) -> str:
    """How to install a command-line program on this system, for error messages."""
    mac, windows, other = _INSTALL_HINTS[program]
    return mac if IS_MAC else windows if IS_WINDOWS else other


def ytdlp_command():
    """
    The command that runs yt-dlp, or None. A yt-dlp on PATH wins; otherwise the package that setup put in the
    virtual environment is run as a module (its launcher sits in .venv/bin or .venv\\Scripts, which isn't on PATH).
    """
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def subprocess_text() -> dict:
    """subprocess.run() options that read a program's output as UTF-8 instead of the system's legacy code page."""
    return {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


def open_path(path, edit: bool = False) -> bool:
    """
    Open a file (in a text editor if `edit`) or a folder with the system's default program.
    False if there is nothing to open it with, e.g. a server without a desktop.
    """
    try:
        if IS_WINDOWS:
            if edit:
                return subprocess.call(["notepad", str(path)]) == 0
            os.startfile(str(path))
            return True
        if IS_MAC:
            return subprocess.call(["open", "-t", str(path)] if edit else ["open", str(path)]) == 0
        return subprocess.call(["xdg-open", str(path)]) == 0
    except OSError:
        return False


def clean_path(text: str) -> str:
    """Accept a path typed, pasted, or dragged in from Finder / File Explorer (quoted, or with '\\ ' on macOS and Linux)."""
    text = text.strip()
    if not text:
        return ""
    if not IS_WINDOWS:  # there a backslash is the folder separator, not an escape
        try:
            parts = shlex.split(text)
            text = parts[0] if len(parts) == 1 else text
        except ValueError:
            pass
    return text.strip("'\"")


def shell_quote(text: str) -> str:
    """Quote one argument for the shell the user would paste it into (cmd/PowerShell on Windows, else sh)."""
    return subprocess.list2cmdline([text]) if IS_WINDOWS else shlex.quote(text)


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def windows_safe(name: str) -> str:
    """On Windows a file or folder can't be called CON, NUL, COM1... (with or without an extension): add an underscore."""
    stem, dot, extension = name.partition(".")
    if IS_WINDOWS and stem.rstrip().upper() in _WINDOWS_RESERVED:
        return f"{stem}_{dot}{extension}"
    return name


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
def _prepare_streams() -> None:
    """
    Song titles come in every script, so printing one must never crash: a character the terminal can't show becomes
    "?". On Windows, output that is piped or redirected would otherwise be cp1252, so it is UTF-8 there.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", **({"encoding": "utf-8"} if IS_WINDOWS else {}))
        except (AttributeError, ValueError, OSError):
            pass  # not a real text stream (a test's StringIO, a closed pipe)


def _enable_windows_ansi() -> bool:
    """Turn on escape-code handling in a Windows console (Windows Terminal has it on already). False if it won't."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetStdHandle.argtypes, kernel32.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        ok = True
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):  # 0 when it's a pipe or file: nothing to do
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)) and ok  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return ok
    except Exception:
        return False


_prepare_streams()
# ANSI: the terminal understands escape codes (cursor moves, clearing); COLOR: and the user hasn't asked for plain text
ANSI = sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and (not IS_WINDOWS or _enable_windows_ansi())
COLOR = ANSI and not os.environ.get("NO_COLOR")


def _c(code):
    return (lambda s: f"\x1b[{code}m{s}\x1b[0m") if COLOR else (lambda s: s)


bold, dim, cyan, green, red, yellow = _c("1"), _c("2"), _c("36"), _c("32"), _c("31"), _c("33")
pink, violet = _c("38;5;205"), _c("38;5;135")


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
