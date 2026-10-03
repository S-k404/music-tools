"""
Translation for the lyrics tool: several services behind one interface.

Services are tried in the order you list in `lyrics.translators`. If one is
down, rate limited or out of quota, the next is used, and a service that keeps
failing is left alone for the rest of the run instead of being hammered.

  google     Google's free web translator. No account or key. (unofficial, may throttle)
  mymemory   MyMemory's free translator. No key, but only a few songs a day. Good as a backup.
  deepl      DeepL. Needs a key in $DEEPL_API_KEY (the free plan works).
  claude     Claude. Needs a key in $ANTHROPIC_API_KEY. Translates with the whole song as context.

Keys are only ever read from the environment, never stored in config.toml.
"""

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass

from common import STOP, fetch_url
from lyrics_lang import norm
from lyrics_unromanize import romaji_to_kana

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class NetError(Exception):
    """A web request failed. `fatal` means retrying (or trying again this run) is pointless."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class TranslateError(NetError):
    """This service couldn't translate right now."""


class Stopped(Exception):
    """Ctrl-C was pressed."""


def request_json(url: str, what: str, data=None, headers=None, timeout: float = 20, retries: int = 2,
                 keyed: bool = False):
    """
    GET (or POST when `data` is given) and return the parsed JSON, or None on a
    404. Rate limits and server errors are retried with a pause; anything else
    raises NetError with a message that says what went wrong in plain words.
    `keyed` marks services that need an API key: a refusal then means the key is wrong.
    """
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    hdrs = {"User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if isinstance(data, (dict, list)) else {}),
            **(headers or {})}
    problem, pause = "", 0.0
    for attempt in range(retries + 1):
        if STOP.is_set():
            raise Stopped()
        try:
            raw = fetch_url(url, data=body, headers=hdrs, timeout=timeout)
            try:
                return json.loads(raw)
            except ValueError:
                raise NetError(f"{what} sent something unexpected (blocked or rate limited?)") from None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if keyed and e.code in (401, 403):
                raise NetError(f"{what} refused the request (HTTP {e.code}); check the API key", fatal=True) from None
            if e.code == 456:
                raise NetError(f"{what}: quota used up", fatal=True) from None
            if e.code not in (429, 500, 502, 503, 504):
                raise NetError(f"{what} answered HTTP {e.code}") from None
            problem = f"{what} is busy (HTTP {e.code})"
            wait = e.headers.get("Retry-After", "") if e.headers else ""
            pause = min(float(wait), 20) if wait.replace(".", "", 1).isdigit() else 1.5 * (attempt + 1) ** 2
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            problem = f"can't reach {what} ({getattr(e, 'reason', e)}); are you online?"
            pause = 1.5 * (attempt + 1)
        if attempt < retries:
            time.sleep(pause)
    raise NetError(problem)


def _clean(s: str) -> str:
    return " ".join(html.unescape(str(s)).split())


# ---------------------------------------------------------------- services
class Backend:
    name = ""
    gap = 0.0  # minimum seconds between requests, to stay polite

    def __init__(self):
        self._lock, self._last = threading.Lock(), 0.0

    def unavailable(self) -> str:
        """Why this service can't be used (e.g. a missing key), or '' when it can."""
        return ""

    def _pace(self):
        with self._lock:
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def translate(self, lines: list, source: str) -> tuple:
        """-> (English lines, same count and order; detected language code or '')."""
        raise NotImplementedError


class Google(Backend):
    name = "google"
    gap = 0.4
    URL = "https://clients5.google.com/translate_a/t"
    MAX_ENCODED = 5000  # keeps the request URL comfortably short

    CODES = {"zh": "zh-CN"}

    def _call(self, text: str, source: str) -> tuple:
        self._pace()
        query = urllib.parse.urlencode({"client": "dict-chrome-ex", "sl": self.CODES.get(source, source or "auto"),
                                        "tl": "en", "q": text})
        data = request_json(f"{self.URL}?{query}", "Google Translate")
        if not isinstance(data, list) or not data:
            raise TranslateError("Google Translate sent an answer we can't read")
        parts, lang = [], ""
        for item in data:
            if isinstance(item, list) and item:
                parts.append(str(item[0]))
                lang = lang or (str(item[1]) if len(item) > 1 else "")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts), lang.split("-")[0]

    def translate(self, lines, source):
        out, lang, chunk, size = [], "", [], 0

        def flush():
            nonlocal lang
            if not chunk:
                return
            text, detected = self._call("\n".join(chunk), source)
            pieces = text.split("\n")
            if len(pieces) != len(chunk):  # lines got merged or split: ask again one line at a time
                pieces = [self._call(line, source)[0] for line in chunk]
            out.extend(_clean(p) for p in pieces)
            lang = lang or detected

        for line in lines:
            cost = len(urllib.parse.quote(line)) + 3
            if chunk and size + cost > self.MAX_ENCODED:
                flush()
                chunk, size = [], 0
            chunk.append(line)
            size += cost
        flush()
        return out, lang


class MyMemory(Backend):
    name = "mymemory"
    gap = 0.5
    URL = "https://api.mymemory.translated.net/get"
    MAX_BYTES = 480  # MyMemory refuses anything over 500 bytes per request

    def translate(self, lines, source):
        out, lang = [], ""
        pair = f"{source if source not in ('', 'auto', 'und') else 'Autodetect'}|en"
        for line in lines:
            if len(line.encode()) > self.MAX_BYTES:
                raise TranslateError("MyMemory can't take lines this long")
            self._pace()
            data = request_json(f"{self.URL}?{urllib.parse.urlencode({'q': line, 'langpair': pair})}", "MyMemory")
            if not isinstance(data, dict):
                raise TranslateError("MyMemory sent an answer we can't read")
            text = str((data.get("responseData") or {}).get("translatedText", ""))
            if data.get("quotaFinished") or "MYMEMORY WARNING" in text.upper() or str(data.get("responseStatus")) == "429":
                raise TranslateError("MyMemory's free quota for today is used up", fatal=True)
            if str(data.get("responseStatus", "200")) != "200" or not text:
                raise TranslateError(f"MyMemory couldn't translate ({data.get('responseDetails') or 'no reason given'})")
            out.append(_clean(text))
            if not lang:
                matches = data.get("matches") or []
                lang = str(matches[0].get("source", "")).split("-")[0].lower() if matches and isinstance(matches[0], dict) else ""
        return out, lang


class DeepL(Backend):
    name = "deepl"
    gap = 0.2
    BATCH = 40
    SOURCES = {"ja": "JA", "ko": "KO", "zh": "ZH", "ru": "RU"}

    def __init__(self):
        super().__init__()
        self.key = os.environ.get("DEEPL_API_KEY", "").strip()

    def unavailable(self):
        return "" if self.key else "DEEPL_API_KEY isn't set"

    def translate(self, lines, source):
        host = "api-free.deepl.com" if self.key.endswith(":fx") else "api.deepl.com"
        out, lang = [], ""
        for i in range(0, len(lines), self.BATCH):
            batch = lines[i:i + self.BATCH]
            body = {"text": batch, "target_lang": "EN-US"}
            if source in self.SOURCES:
                body["source_lang"] = self.SOURCES[source]
            self._pace()
            data = request_json(f"https://{host}/v2/translate", "DeepL", data=body,
                                headers={"Authorization": f"DeepL-Auth-Key {self.key}"}, keyed=True)
            items = data.get("translations") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(batch):
                raise TranslateError("DeepL sent an answer we can't read")
            out.extend(_clean(t.get("text", "")) for t in items)
            lang = lang or str(items[0].get("detected_source_language", "")).lower()
        return out, lang


ROMANIZED = "-romanized"   # a source like "ja-romanized": the lines are that language written in Latin letters
_LANGUAGES = {"ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian"}


def describe_source(source: str) -> str:
    if source.endswith(ROMANIZED):
        name = _LANGUAGES.get(source[:-len(ROMANIZED)], "the original language")
        return f"{name} written in Latin letters (romanized, not in its own script): read it as {name}"
    return source if source not in ("", "auto", "und") else "detect it"


class Claude(Backend):
    name = "claude"
    gap = 0.2
    BATCH = 120

    def __init__(self, model: str):
        super().__init__()
        self.model = model
        self.key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def unavailable(self):
        return "" if self.key else "ANTHROPIC_API_KEY isn't set"

    def translate(self, lines, source):
        out, lang = [], ""
        for i in range(0, len(lines), self.BATCH):
            batch = lines[i:i + self.BATCH]
            prompt = (
                "Translate these song lyric lines into natural, faithful English. Keep the meaning and feel; "
                "don't explain or add notes. The lines belong to one song in order, so use the surrounding lines "
                f"for context. Source language: {describe_source(source)}.\n"
                f'Reply with ONLY a JSON object: {{"language": "<ISO 639-1 code of the original>", '
                f'"lines": [<exactly {len(batch)} English strings, one per input line, in order>]}}\n\n'
                f"Input lines (JSON array):\n{json.dumps(batch, ensure_ascii=False)}")
            self._pace()
            data = request_json("https://api.anthropic.com/v1/messages", "Claude", data={
                "model": self.model, "max_tokens": 16384, "messages": [{"role": "user", "content": prompt}]},
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"}, timeout=90, keyed=True)
            try:
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                answer = json.loads(text)
                result = [_clean(x) for x in answer["lines"]]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise TranslateError("Claude's answer wasn't in the expected format") from None
            if len(result) != len(batch):
                raise TranslateError("Claude returned the wrong number of lines")
            out.extend(result)
            lang = lang or str(answer.get("language", "")).lower()
        return out, lang


# ---------------------------------------------------------------- the chain
@dataclass
class Translation:
    lines: list      # English, same length and order as the input
    lang: str        # detected language of the original ('' if unknown)
    backend: str     # which service did it


def looks_translated(source: list, result: list) -> bool:
    """False when a service just handed the text back (blocked, or the wrong language was assumed)."""
    letters = [(a, b) for a, b in zip(source, result) if any(c.isalpha() for c in a)]
    if not letters:
        return True
    changed = sum(1 for a, b in letters if b.strip() and norm(a) != norm(b))
    return changed / len(letters) >= 0.1


class Translator:
    """
    Tries each configured service in turn. A service that fails several times in
    a row is benched for a few minutes (then given one more chance); one whose
    key is refused or quota is used up is benched for the rest of the run.
    """

    STRIKES = 3        # failures in a row before a service is benched
    COOLDOWN = 180     # seconds

    def __init__(self, names: list, claude_model: str = "claude-sonnet-5"):
        makers = {"google": Google, "mymemory": MyMemory, "deepl": DeepL, "claude": lambda: Claude(claude_model)}
        self.backends, self.skipped = [], {}
        for name in dict.fromkeys(names):
            backend = makers[name]()
            why = backend.unavailable()
            if why:
                self.skipped[name] = why
            else:
                self.backends.append(backend)
        self._lock = threading.Lock()
        self._strikes, self._benched = {}, {}   # name -> count; name -> (until, reason)

    @property
    def names(self) -> list:
        return [b.name for b in self.backends]

    def benched(self) -> dict:
        """Services given up on during this run: name -> reason."""
        with self._lock:
            return {n: why for n, (_, why) in self._benched.items()}

    def _live(self) -> list:
        now, live = time.monotonic(), []
        with self._lock:
            for b in self.backends:
                entry = self._benched.get(b.name)
                if entry and entry[0] > now:
                    continue
                if entry:  # cooldown over: one more failure benches it again
                    del self._benched[b.name]
                    self._strikes[b.name] = self.STRIKES - 1
                live.append(b)
        return live

    def _failed(self, backend: Backend, error: NetError) -> None:
        with self._lock:
            self._strikes[backend.name] = self._strikes.get(backend.name, 0) + 1
            if error.fatal:
                self._benched[backend.name] = (float("inf"), str(error))
            elif self._strikes[backend.name] >= self.STRIKES:
                self._benched[backend.name] = (time.monotonic() + self.COOLDOWN, f"kept failing ({error})")

    @staticmethod
    def _prepare(backend: Backend, lines: list, source: str):
        """What to send this service for `source`. Romanized text is written back into kana for the services that
        can't read Latin letters (Japanese only); Claude reads romanization directly; nothing else can."""
        if not source.endswith(ROMANIZED):
            return lines, source
        code = source[:-len(ROMANIZED)]
        if backend.name == "claude":
            return lines, source
        if code == "ja":
            return [romaji_to_kana(l) for l in lines], "ja"
        return None

    def translate(self, lines: list, source: str = "auto", verify: bool = True) -> Translation:
        if not lines:
            return Translation([], "", "")
        live, problems = self._live(), []
        if not live:
            why = "; ".join(f"{n}: {r}" for n, r in self.benched().items())
            raise TranslateError("no translation service is working right now" + (f" ({why})" if why else "")
                                 + ". Try again in a few minutes.")
        for backend in live:
            if STOP.is_set():
                raise Stopped()
            prepared = self._prepare(backend, lines, source)
            if prepared is None:  # a limit of this service, not an outage: no strike against it
                problems.append(f"{backend.name}: can't read romanized "
                                f"{_LANGUAGES.get(source[:-len(ROMANIZED)], 'text')} (only Claude can)")
                continue
            payload, service_source = prepared
            try:
                result, lang = backend.translate(payload, service_source)
                if len(result) != len(lines):
                    raise TranslateError(f"{backend.name} returned the wrong number of lines")
                if verify and not looks_translated(lines, result):
                    raise TranslateError(f"{backend.name} returned the text untranslated")
            except NetError as e:
                problems.append(f"{backend.name}: {e}")
                self._failed(backend, e)
            else:
                with self._lock:
                    self._strikes[backend.name] = 0
                return Translation(result, lang.lower(), backend.name)
        raise TranslateError("; ".join(problems))

    def detect(self, lines: list) -> str:
        """Ask which language these lines are in ('' if no service can say)."""
        # prefer lines with more distinct words: a short vocable ad-lib ("la la la", "oh-oh-oh",
        # repeated at the start of a lot of songs) carries almost no language signal and can get
        # misread as some other language by auto-detect, even though the rest of the song is
        # unambiguous - that wrong guess then sends a plainly-English song through the translator,
        # which correctly refuses to "translate" English into English and makes it look like a
        # service outage.
        candidates = list(dict.fromkeys(l for l in lines if any(c.isalpha() for c in l)))
        probe = sorted(candidates, key=lambda l: -len(set(re.findall(r"[^\W_]+", l.lower()))))[:6]
        try:
            return self.translate(probe, "auto", verify=False).lang.split("-")[0]
        except TranslateError:
            return ""
