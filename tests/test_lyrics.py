"""Tests for the lyrics tool: language detection, matching, layers, and the safety rules (nothing of yours is
ever changed without asking, nothing is half-saved, everything can be retried). No network is used."""

import contextlib
import io
import os
import shutil
import importlib.util
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import find_lyrics  # noqa: E402
import lyrics_fetch  # noqa: E402
import lyrics_lang  # noqa: E402
import lyrics_translate  # noqa: E402
import lyrics_unromanize  # noqa: E402
import lyrics_local  # noqa: E402
from lyrics_render import (Line, parse_html_data, parse_layers, render_html, render_lrc, render_terminal,  # noqa: E402
                           render_txt)
from lyrics_translate import Translation, TranslateError  # noqa: E402


class LanguageTests(unittest.TestCase):
    def test_the_writing_decides_the_language(self):
        self.assertEqual(lyrics_lang.guess_language("夢ならばどれほどよかったでしょう"), "ja")
        self.assertEqual(lyrics_lang.guess_language("사랑해 너를\n오늘도 너를 기다려"), "ko")
        self.assertEqual(lyrics_lang.guess_language("我爱你 中国\n月亮代表我的心"), "zh")
        self.assertEqual(lyrics_lang.guess_language("Я тебя люблю\nТы моя судьба"), "ru")
        self.assertEqual(lyrics_lang.guess_language("I love you baby\nYou know that I do, oh yeah"), "en")
        self.assertEqual(lyrics_lang.guess_language("Te quiero mucho mi amor\nNo puedo vivir sin ti"), "und")
        self.assertEqual(lyrics_lang.guess_language("♪ ♪ ♪"), "en")  # nothing to translate

    def test_decomposed_korean_from_macos_is_still_korean(self):
        self.assertEqual(lyrics_lang.guess_language(unicodedata.normalize("NFD", "사랑해 너를")), "ko")

    def test_a_little_english_in_a_korean_song_doesnt_change_it(self):
        text = "\n".join(["사랑해 너를", "오늘도 기다려", "정말 좋아", "Yeah baby"])
        self.assertEqual(lyrics_lang.guess_language(text), "ko")

    def test_each_line_gets_its_own_romanizer(self):
        self.assertEqual(lyrics_lang.line_language("夢", "ja"), "ja")
        self.assertEqual(lyrics_lang.line_language("夢", "ko"), "")
        self.assertEqual(lyrics_lang.line_language("안녕", "ja"), "ko")
        self.assertEqual(lyrics_lang.line_language("hello", "ja"), "")
        self.assertEqual(lyrics_lang.line_language("привет", "ja"), "ru")


class UnromanizeTests(unittest.TestCase):
    def test_romaji_becomes_kana(self):
        k = lyrics_unromanize.romaji_to_kana
        for romaji, kana in [
            ("Yume naraba dore hodo yokatta deshou", "ゆめならばどれほどよかったでしょう"),
            ("Imada ni anata no koto wo yume ni miru", "いまだにあなたのことをゆめにみる"),
            ("watashi wa gakusei desu", "わたしはがくせいです"),      # the particle wa is written は
            ("Tōkyō e ikou", "とうきょうへいこう"),                     # macrons; the particle e is written へ
            ("Kekkon shite", "けっこんして"),                           # doubled consonant: small tsu
            ("matcha ga suki", "まっちゃがすき"),
            ("kannai", "かんない"),
            ("kin'iro", "きんいろ"),                                    # n' keeps ん apart from the next vowel
            ("ra-men", "らーめん"),
            ("Kimi no na wa", "きみのなは"),
        ]:
            with self.subTest(romaji=romaji):
                self.assertEqual(k(romaji), kana)

    def test_english_is_left_alone(self):
        k = lyrics_unromanize.romaji_to_kana
        self.assertEqual(k("Baby tell me why"), "Baby tell me why")
        self.assertEqual(k("I love you so much"), "I love you so much")
        self.assertEqual(k("Lemon no nioi"), "Lemonのにおい")   # one English word inside a romaji line
        self.assertEqual(k(""), "")


class UntranslatableTests(unittest.TestCase):
    def chain(self, outcomes):
        """Services that each either hand the lines back, raise, or translate; outcomes = {name: "same"|"distinct"|"busy"|"ok"}."""
        def make(name, outcome):
            class Fake(lyrics_translate.Backend):
                def translate(self, lines, source):
                    if outcome == "same":
                        return list(lines), "en"
                    if outcome == "distinct":
                        raise TranslateError("MyMemory couldn't translate (PLEASE SELECT TWO DISTINCT LANGUAGES)")
                    if outcome == "busy":
                        raise TranslateError(f"{name} is busy")
                    return [f"EN {l}" for l in lines], "de"
            b = Fake()
            b.name = name
            return b
        t = lyrics_translate.Translator([])
        t.backends = [make(n, o) for n, o in outcomes.items()]
        return t

    def test_all_services_saying_same_language_is_untranslatable_and_nobody_is_benched(self):
        t = self.chain({"google": "same", "mymemory": "distinct"})
        with self.assertRaises(lyrics_translate.Untranslatable):
            t.translate(["hold on to the light"], "auto")
        self.assertEqual(t.benched(), {})
        self.assertEqual(t._strikes, {})

    def test_a_service_having_trouble_keeps_it_an_ordinary_failure(self):
        t = self.chain({"google": "same", "mymemory": "busy"})
        with self.assertRaises(TranslateError) as ctx:
            t.translate(["hold on to the light"], "auto")
        self.assertNotIsInstance(ctx.exception, lyrics_translate.Untranslatable)

    def test_a_service_that_translates_wins(self):
        t = self.chain({"google": "same", "mymemory": "ok"})
        self.assertEqual(t.translate(["Hallo Welt"], "auto").lines, ["EN Hallo Welt"])


class RomanizedChainTests(unittest.TestCase):
    def chain(self, *names, fail=()):
        seen = {}

        def make(name):
            class Fake(lyrics_translate.Backend):
                def translate(self, lines, source):
                    seen[name] = (list(lines), source)
                    if name in fail:
                        raise TranslateError(f"{name} is busy")
                    return [f"EN{i}" for i in range(len(lines))], "ja"
            b = Fake()
            b.name = name
            return b
        t = lyrics_translate.Translator([])
        t.backends = [make(n) for n in names]
        return t, seen

    def test_services_that_cant_read_romaji_are_sent_kana(self):
        t, seen = self.chain("google", "claude")
        t.translate(["Yume naraba"], "ja-romanized")
        self.assertEqual(seen, {"google": (["ゆめならば"], "ja")})

    def test_claude_reads_the_romanization_itself(self):
        t, seen = self.chain("google", "claude", fail=("google",))
        t.translate(["Yume naraba"], "ja-romanized")
        self.assertEqual(seen["claude"], (["Yume naraba"], "ja-romanized"))

    def test_romanized_korean_skips_services_that_cant_read_it_without_blaming_them(self):
        t, seen = self.chain("google")
        with self.assertRaises(TranslateError) as ctx:
            t.translate(["saranghae"], "ko-romanized")
        self.assertIn("only Claude", str(ctx.exception))
        self.assertEqual(seen, {})
        self.assertEqual(t.benched(), {})   # a limit of the service isn't an outage

    def test_romanized_russian_skips_services_that_cant_read_it_without_blaming_them(self):
        t, seen = self.chain("google")
        with self.assertRaises(TranslateError) as ctx:
            t.translate(["privet"], "ru-romanized")
        self.assertIn("only Claude", str(ctx.exception))
        self.assertEqual(seen, {})
        self.assertEqual(t.benched(), {})

    def test_claude_reads_romanized_russian_itself(self):
        t, seen = self.chain("google", "claude", fail=("google",))
        t.translate(["privet"], "ru-romanized")
        self.assertEqual(seen["claude"], (["privet"], "ru-romanized"))

    def test_detect_prefers_lines_with_more_distinct_words(self):
        # a repeated one-syllable ad-lib ("la la la") carries almost no language signal and can
        # get auto-detect to guess the wrong language for an otherwise clearly-English song; the
        # probe sent for detection should prefer the lines that actually say something
        t, seen = self.chain("google")
        lines = ["la la la la la la", "la la la la la la", "I ain't afraid of anything fictional tonight"]
        t.detect(lines)
        probe, source = seen["google"]
        self.assertEqual(probe, ["I ain't afraid of anything fictional tonight", "la la la la la la"])
        self.assertEqual(source, "auto")


class BilingualLrcTests(unittest.TestCase):
    def pairs(self, second):
        out = []
        for i, (a, b) in enumerate(zip(["夢ならば", "未だに", "忘れた物", "古びた"], second)):
            out += [Line(i * 5.0, a), Line(i * 5.0, b)]
        return out

    def test_a_translation_into_another_language_is_dropped(self):
        lines = lyrics_lang.fold_layers(self.pairs(["Giá như tất cả chỉ là một giấc mơ", "Đến giờ anh vẫn mơ thấy em",
                                                      "Như quay về để lấy lại món đồ", "Phủi đi lớp bụi trên ký ức"]))
        self.assertEqual([l.text for l in lines], ["夢ならば", "未だに", "忘れた物", "古びた"])
        self.assertTrue(all(not l.english and not l.romaji for l in lines))

    def test_an_english_second_line_becomes_the_english_layer(self):
        lines = lyrics_lang.fold_layers(self.pairs(["If it were a dream", "I still dream about you",
                                                      "Like going home to get what I forgot", "Dust off old memories"]))
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[1].english, "I still dream about you")

    def test_a_romanized_second_line_becomes_the_romanization(self):
        lines = lyrics_lang.fold_layers(self.pairs(["Yume naraba", "Imada ni", "Wasureta mono wo", "Furubita omoide"]))
        self.assertEqual(lines[0].romaji, "Yume naraba")
        self.assertEqual(lines[0].english, "")

    def test_ordinary_lyrics_are_left_alone(self):
        lines = [Line(1.0, "a"), Line(2.0, "b"), Line(3.0, "c")]
        self.assertEqual(lyrics_lang.fold_layers(lines), lines)

    def test_credit_lines_and_title_header_are_removed(self):
        lines = [Line(0.0, "作词 : 米津玄師"), Line(0.2, "作曲 : 米津玄師"), Line(0.4, "Lemon - Kenshi Yonezu"),
                 Line(1.0, "夢ならばどれほど")]
        kept = lyrics_fetch.strip_credits(lines, "Kenshi Yonezu", "Lemon")
        self.assertEqual([l.text for l in kept], ["夢ならばどれほど"])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.old = lyrics_fetch.api_get
        self.addCleanup(setattr, lyrics_fetch, "api_get", self.old)

    def api(self, get=None, search=None, title_only=None):
        def fake(path, **params):
            if path == "get":
                return get
            if "artist_name" not in params and title_only is not None:
                return title_only
            return search or []
        lyrics_fetch.api_get = fake

    def test_the_same_title_by_a_different_artist_is_not_accepted(self):
        self.api(search=[{"artistName": "U2", "trackName": "Lemon", "duration": 320, "plainLyrics": "wrong song"}])
        with self.assertRaises(lyrics_fetch.NotFound):
            lyrics_fetch.fetch("Kenshi Yonezu", "Lemon", duration=256)

    def test_an_artist_lrclib_spells_in_kanji_is_matched_by_length(self):
        self.api(search=[{"artistName": "米津玄師", "trackName": "Lemon", "duration": 256,
                          "syncedLyrics": "[00:01.00]夢\n[00:02.00]花"}])
        result = lyrics_fetch.fetch("Kenshi Yonezu", "Lemon", duration=256)
        self.assertTrue(result.synced)

    def test_a_recording_of_a_different_length_is_not_used(self):
        self.api(search=[{"artistName": "Kenshi Yonezu", "trackName": "Lemon", "duration": 500, "plainLyrics": "live"}])
        with self.assertRaises(lyrics_fetch.NotFound):
            lyrics_fetch.fetch("Kenshi Yonezu", "Lemon", duration=256)

    def test_synced_beats_plain_for_the_same_recording(self):
        self.api(search=[{"artistName": "IU", "trackName": "Palette", "duration": 200, "plainLyrics": "plain"},
                         {"artistName": "IU", "trackName": "Palette", "duration": 201, "syncedLyrics": "[00:01.00]synced"}])
        self.assertTrue(lyrics_fetch.fetch("IU", "Palette", duration=200).synced)

    def test_timestamps_from_a_different_length_recording_are_not_trusted(self):
        self.api(search=[{"artistName": "IU", "trackName": "Palette", "duration": 209,
                          "syncedLyrics": "[00:01.00]synced", "plainLyrics": "plain"}])
        result = lyrics_fetch.fetch("IU", "Palette", duration=200)
        self.assertFalse(result.synced)
        self.assertEqual(result.lines[0].text, "plain")

    def test_without_an_artist_or_a_length_it_will_not_guess(self):
        self.api(search=[{"artistName": "U2", "trackName": "Lemon", "plainLyrics": "x"}])
        with self.assertRaises(lyrics_fetch.NotFound) as ctx:
            lyrics_fetch.fetch("", "Lemon")
        self.assertIn("no artist", str(ctx.exception))

    def test_the_original_writing_is_preferred_over_a_romanization(self):
        romaji = {"artistName": "Kenshi Yonezu", "trackName": "Lemon", "duration": 256,
                  "syncedLyrics": "[00:01.00]Yume naraba dore hodo yokatta deshou\n[00:06.00]Imada ni anata no koto wo"}
        kanji = {"artistName": "米津玄師", "trackName": "Lemon", "duration": 256,
                 "syncedLyrics": "[00:01.00]夢ならばどれほどよかったでしょう\n[00:06.00]未だにあなたのことを夢にみる"}
        self.api(search=[romaji], title_only=[kanji])
        self.assertEqual(lyrics_fetch.fetch("Kenshi Yonezu", "Lemon", duration=256).lines[0].text,
                         "夢ならばどれほどよかったでしょう")

    def test_a_service_that_cant_be_reached_is_not_reported_as_not_found(self):
        def down(path, **params):
            raise lyrics_fetch.LyricsError("can't reach lrclib.net")
        lyrics_fetch.api_get = down
        with self.assertRaises(lyrics_fetch.LyricsError) as ctx:
            lyrics_fetch.fetch("A", "B", duration=100)
        self.assertNotIsInstance(ctx.exception, lyrics_fetch.NotFound)

    def test_a_rejected_exact_match_still_lets_search_find_it(self):
        # lrclib's "get" endpoint can hard-reject a request (e.g. a duration outside what it
        # accepts) with its own error instead of just not matching. That's a dead end for the
        # exact-match shortcut, not a reason to give up on the whole lookup - search should still run.
        hit = {"artistName": "A", "trackName": "B", "duration": 4000, "plainLyrics": "found it"}

        def fake(path, **params):
            if path == "get":
                raise lyrics_fetch.LyricsError("lrclib.net answered HTTP 400")
            return [hit]
        lyrics_fetch.api_get = fake
        result = lyrics_fetch.fetch("A", "B", duration=4000)
        self.assertEqual(result.lines[0].text, "found it")

    def test_a_too_long_duration_is_not_sent_to_the_exact_match_endpoint(self):
        # lrclib's "get" endpoint rejects any duration outside 1-3600s with a hard validation
        # error; a file that long is a mix/compilation, not a song, so don't even ask for one.
        seen = []

        def fake(path, **params):
            if path == "get":
                seen.append(params.get("duration"))
            return None if path == "get" else []
        lyrics_fetch.api_get = fake
        with self.assertRaises(lyrics_fetch.NotFound):
            lyrics_fetch.fetch("A", "B", duration=4000)   # over an hour
        self.assertEqual(seen, [None])

    def test_tidy_title_keeps_a_title_thats_entirely_in_brackets(self):
        # a title with nothing outside its own brackets ("(Original / Romanized)", some Vocaloid
        # songs are tagged like this) is the real title, not decoration: stripping it blindly
        # would leave nothing to search lrclib with
        self.assertEqual(lyrics_fetch.tidy_title("(Dadadadadaru / Amala)"), "Dadadadadaru / Amala")
        self.assertEqual(lyrics_fetch.tidy_title("Song Title (feat. Other) [Official Video]"), "Song Title")

    def test_fetch_tries_the_tidied_title_when_the_whole_title_is_bracketed(self):
        hit = {"artistName": "Nova", "trackName": "Rise Up", "duration": 160, "syncedLyrics": "[00:01.00]line"}

        def fake(path, **params):
            if path == "get":
                return None
            return [hit] if params.get("track_name") == "Rise Up" else []
        lyrics_fetch.api_get = fake
        result = lyrics_fetch.fetch("Nova", "(Rise Up)", duration=160)
        self.assertEqual(result.lines[0].text, "line")


class LocalLyricsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_messy_lrc_files_are_understood(self):
        text = ("﻿[ti:Song]\n[offset:500]\n[00:10.00]<00:10.00>Hel<00:10.50>lo\n"
                "[00:20.00][01:00.00]Chorus\n[00:30.5]Second\n")
        lines, synced = lyrics_local.parse_lyrics(text)
        self.assertTrue(synced)
        self.assertEqual([(l.time, l.text) for l in lines],
                         [(9.5, "Hello"), (20.0 - 0.5, "Chorus"), (30.0, "Second"), (60.0 - 0.5, "Chorus")])

    def test_older_files_in_shift_jis_euc_kr_or_gbk_are_read_correctly(self):
        for encoding, text in (("cp932", "夢ならばどれほどよかったでしょう\n未だにあなたのことを夢にみる"),
                               ("euc-kr", "이 밤 그날의 반딧불을 당신의\n창 가까이 보낼게요"),
                               ("gb18030", "月亮代表我的心\n你问我爱你有多深")):
            with self.subTest(encoding=encoding):
                self.assertEqual(lyrics_local.decode_text(text.encode(encoding)), text)
        self.assertEqual(lyrics_local.decode_text("café 夢".encode("utf-8")), "café 夢")
        self.assertEqual(lyrics_local.decode_text("夢".encode("utf-16")), "夢")

    def test_plain_lyrics_are_tidied(self):
        lines, synced = lyrics_local.parse_lyrics("\n\nOne\nTwo\n\n\n\nThree\n")
        self.assertFalse(synced)
        self.assertEqual([l.text for l in lines], ["One", "Two", "", "Three"])

    def test_a_lrc_written_by_music_tools_is_not_a_source_of_lyrics(self):
        song = self.dir / "a.mp3"
        song.write_bytes(b"")
        (self.dir / "a.lrc").write_text(render_lrc([Line(1.0, "x", english="y"), Line(2.0, "z")], "T", "A"))
        self.assertIsNone(lyrics_local.local_lyrics(song))
        (self.dir / "a.lrc").write_text("[00:01.00]mine\n[00:02.00]too\n")
        self.assertEqual(lyrics_local.local_lyrics(song).lines[0].text, "mine")

    def test_lyrics_stored_in_the_tags_are_found(self):
        from mutagen.id3 import ID3, USLT
        song = self.dir / "a.mp3"
        song.write_bytes(b"")
        tags = ID3()
        tags.add(USLT(encoding=3, lang="eng", desc="", text="Line one\nLine two"))
        tags.save(song)
        found = lyrics_local.local_lyrics(song)
        self.assertEqual([l.text for l in found.lines], ["Line one", "Line two"])
        self.assertEqual(found.source, "the song's tags")


class LayerTests(unittest.TestCase):
    lines = [Line(1.0, "夢", "Yume", "dream"), Line(2.0, ""), Line(3.0, "plain english", "", "")]

    def test_names_and_aliases(self):
        self.assertEqual(parse_layers("kanji + romaji"), ["original", "romanization"])
        self.assertEqual(parse_layers("english,original"), ["original", "english"])
        self.assertEqual(parse_layers(["translation"]), ["english"])
        for bad in ("", "kanjii", "original,nonsense"):
            with self.assertRaises(ValueError):
                parse_layers(bad)

    def test_each_pair_in_the_lrc(self):
        both = render_lrc(self.lines, layers=["romanization", "english"])
        self.assertIn("[00:01.00]Yume\n[00:01.00]dream", both)
        self.assertNotIn("夢", both)
        pair = render_lrc(self.lines, layers=["original", "english"])
        self.assertIn("[00:01.00]夢\n[00:01.00]dream", pair)
        self.assertNotIn("Yume", pair)

    def test_a_line_with_nothing_in_the_chosen_layers_still_shows_its_original(self):
        text = render_lrc(self.lines, layers=["romanization", "english"])
        self.assertIn("[00:03.00]plain english", text)
        self.assertIn("plain english", render_txt(self.lines, layers=["english"]))

    def test_the_page_keeps_every_layer_and_can_be_read_back(self):
        page = render_html(self.lines, "T", "A", "ja", "lrclib.net", "s.mp3", layers=["original", "english"])
        self.assertIn('data-show="o,e"', page)
        self.assertEqual(page.count("data-cols="), 4)          # all, o+e, r+e, o+r
        self.assertIn('content="music-tools"', page)
        data = parse_html_data(page)
        self.assertEqual([(l.time, l.text, l.romaji, l.english) for l in data["lines"]],
                         [(l.time, l.text, l.romaji, l.english) for l in self.lines])

    def test_terminal_view_shows_only_the_chosen_layers(self):
        text = "\n".join(render_terminal(self.lines, 100, layers=["original", "english"]))
        self.assertIn("dream", text)
        self.assertNotIn("Yume", text)


class ToolSafetyTests(unittest.TestCase):
    """The whole tool, run against a temporary library with the network replaced by fakes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        for name in ("fetch", "Translator", "CHECKED_FILE"):
            self.addCleanup(setattr, find_lyrics, name, getattr(find_lyrics, name))
        find_lyrics.CHECKED_FILE = self.dir / "checked.json"
        self.lib = self.dir / "lib"
        self.lib.mkdir()
        self.config = self.dir / "c.toml"
        self.config.write_text(f'music_dir = "{self.lib}"\nsave_logs = false\n')
        self.fetches = 0

    def song(self, name="Artist - Title.mp3"):
        p = self.lib / name
        p.write_bytes(b"")
        return p

    def lyrics(self, lines, synced=True, error=None):
        def fake(artist, title, album="", duration=None):
            self.fetches += 1
            if error:
                raise error
            return lyrics_fetch.Lyrics([Line(l.time, l.text) for l in lines], synced, artist, title)
        find_lyrics.fetch = fake

    def translator(self, lang="ko", fail=None, names=("fake",)):
        outer = self
        self.sources = []
        service_names = list(names)   # (a class body can't see the enclosing function's variables by the same name)

        class Fake:
            skipped = {}

            def __init__(self, *a, **k):
                pass

            names = service_names

            def detect(self, texts):
                return lang

            def benched(self):
                return {}

            def translate(self, texts, source):
                outer.sources.append(source)
                if fail:
                    raise fail
                outer.translated = list(texts)
                return Translation([f"EN:{t}" for t in texts], lang, "fake")
        find_lyrics.Translator = Fake

    def run_tool(self, *argv):
        old_argv, old_stdin, out = sys.argv, sys.stdin, io.StringIO()
        sys.argv = ["find_lyrics.py", "--config", str(self.config), "--no-progress", *argv]
        sys.stdin = io.StringIO("")   # not a terminal: the tool must never wait for an answer
        try:
            with contextlib.redirect_stdout(out):
                try:
                    find_lyrics.main()
                except SystemExit as e:
                    out.write(f"[exit {e.code}]")
        finally:
            sys.argv, sys.stdin = old_argv, old_stdin
        return out.getvalue()

    KOREAN = [Line(1.0, "안녕"), Line(3.0, "사랑해"), Line(5.0, "안녕")]

    def test_a_lrc_you_made_is_not_changed_without_asking(self):
        song = self.song()
        mine = "[00:01.00]안녕\n[00:03.00]사랑해\n"
        (self.lib / "Artist - Title.lrc").write_text(mine)
        self.translator("ko")
        self.lyrics([], error=lyrics_fetch.NotFound("nothing"))
        out = self.run_tool()
        self.assertEqual((self.lib / "Artist - Title.lrc").read_text(), mine)
        self.assertFalse((self.lib / "Artist - Title.lrc.bak").exists())
        self.assertTrue((self.lib / "Artist - Title.html").is_file())   # the page is new, so it's fine
        self.assertIn("left as", out)
        # ...and the song is offered again next time instead of being forgotten
        self.assertFalse(find_lyrics.has_lyrics(song, ["lrc", "html"], find_lyrics.load_checked()))

    def test_yes_adds_the_translation_into_your_lrc_and_keeps_your_original(self):
        self.song()
        mine = "[00:01.00]안녕\n[00:03.00]사랑해\n"
        target = self.lib / "Artist - Title.lrc"
        target.write_text(mine)
        self.translator("ko")
        self.lyrics([], error=lyrics_fetch.NotFound("nothing"))
        self.run_tool("--yes")
        text = target.read_text()
        self.assertIn("[00:01.00]안녕\n[00:01.00]annyeong\n[00:01.00]EN:안녕", text)
        self.assertIn("[by:music-tools", text)
        self.assertEqual((self.lib / "Artist - Title.lrc.bak").read_text(), mine)
        self.run_tool("--yes")   # running again doesn't pile up backups or re-translate a finished song
        self.assertEqual((self.lib / "Artist - Title.lrc.bak").read_text(), mine)

    def test_backup_dir_collects_bak_files_in_one_place_mirroring_the_library(self):
        self.song("Artist - Title.mp3")
        mine = "[00:01.00]안녕\n[00:03.00]사랑해\n"
        target = self.lib / "Artist - Title.lrc"
        target.write_text(mine)
        backups = self.dir / "backups"
        self.config.write_text(
            f'music_dir = "{self.lib}"\nsave_logs = false\n\n[lyrics]\nbackup_dir = "{backups}"\n')
        self.translator("ko")
        self.lyrics([], error=lyrics_fetch.NotFound("nothing"))
        self.run_tool("--yes")
        self.assertFalse((self.lib / "Artist - Title.lrc.bak").exists())   # not scattered into the library
        self.assertEqual((backups / "Artist - Title.lrc.bak").read_text(), mine)
        self.assertNotEqual(target.read_text(), mine)   # the translation was still added to the .lrc itself

    def test_restore_lrc_puts_your_original_back_and_removes_the_backup(self):
        self.song("Artist - Title.mp3")
        mine = "[00:01.00]안녕\n[00:03.00]사랑해\n"
        target = self.lib / "Artist - Title.lrc"
        target.write_text(mine)
        self.translator("ko")
        self.lyrics([], error=lyrics_fetch.NotFound("nothing"))
        self.run_tool("--yes")
        self.assertNotEqual(target.read_text(), mine)   # the translation was added
        out = self.run_tool("--restore-lrc")
        self.assertEqual(target.read_text(), mine)
        self.assertFalse((self.lib / "Artist - Title.lrc.bak").exists())
        self.assertIn("Artist - Title.lrc", out)

    def test_restore_lrc_dry_run_previews_without_changing_anything(self):
        self.song("Artist - Title.mp3")
        mine = "[00:01.00]안녕\n[00:03.00]사랑해\n"
        target = self.lib / "Artist - Title.lrc"
        target.write_text(mine)
        self.translator("ko")
        self.lyrics([], error=lyrics_fetch.NotFound("nothing"))
        self.run_tool("--yes")
        translated = target.read_text()
        self.run_tool("--restore-lrc", "--dry-run")
        self.assertEqual(target.read_text(), translated)   # unchanged
        self.assertTrue((self.lib / "Artist - Title.lrc.bak").exists())   # backup still there

    def test_only_foreign_songs_are_saved_by_default(self):
        self.song()
        self.lyrics([Line(1.0, "I love you baby"), Line(3.0, "You know that I do")])
        self.translator("en")
        out = self.run_tool()
        self.assertFalse(list(self.lib.glob("*.html")) or list(self.lib.glob("*.lrc")))
        self.assertIn("already English", out)
        self.run_tool("--include-english")   # asking for it wins over what was remembered
        self.assertTrue(list(self.lib.glob("*.html")))

    def test_repeated_lines_are_translated_once(self):
        self.song()
        self.lyrics(self.KOREAN)
        self.translator("ko")
        self.run_tool()
        self.assertEqual(self.translated, ["안녕", "사랑해"])

    ROMAJI = [Line(1.0, "Yume naraba dore hodo yokatta deshou"), Line(3.0, "Imada ni anata no koto wo yume ni miru"),
              Line(5.0, "Modoranai shiawase ga aru koto wo")]
    ROMANIZED_KOREAN = [Line(1.0, "i bam geunarui banditbureul dangsinui"), Line(3.0, "chang gakkai bonaelgeyo eum"),
                        Line(5.0, "saranghandaneun marieyo")]
    ROMANIZED_RUSSIAN = [Line(1.0, "privet moya lyubov"), Line(3.0, "ya tebya lyublyu")]

    def test_lyrics_that_are_already_romanized_get_translated(self):
        self.song()
        self.lyrics(self.ROMAJI)
        self.translator("ja")   # what a service says about romaji: Japanese
        out = self.run_tool()
        self.assertEqual(self.sources, ["ja-romanized"])
        lrc = (self.lib / "Artist - Title.lrc").read_text()
        self.assertIn("[00:01.00]Yume naraba dore hodo yokatta deshou\n[00:01.00]EN:Yume naraba dore hodo yokatta deshou", lrc)
        self.assertNotIn("annyeong", lrc)
        page = (self.lib / "Artist - Title.html").read_text()
        self.assertIn('lang="ja-Latn"', page)
        self.assertIn("(romanized)", page)
        self.assertIn("from romanized text", out)

    def test_translating_romanized_lyrics_can_be_switched_off(self):
        self.song()
        self.lyrics(self.ROMAJI)
        self.translator("ja")
        out = self.run_tool("--no-romanized")
        self.assertIn("switched off", out)
        self.assertEqual(list(self.lib.glob("*.html")), [])
        self.assertEqual(self.sources, [])

    def test_romanized_korean_needs_claude_and_says_so(self):
        self.song()
        self.lyrics(self.ROMANIZED_KOREAN)
        self.translator("ko")
        out = self.run_tool()
        self.assertIn("only Claude", out)
        self.assertEqual(list(self.lib.glob("*.html")), [])
        self.assertEqual(find_lyrics.load_checked(), {})   # not remembered: a key added later should work

    def test_romanized_korean_is_translated_when_claude_is_available(self):
        self.song()
        self.lyrics(self.ROMANIZED_KOREAN)
        self.translator("ko", names=("google", "claude"))
        self.run_tool()
        self.assertEqual(self.sources, ["ko-romanized"])
        self.assertIn("EN:saranghandaneun marieyo", (self.lib / "Artist - Title.lrc").read_text())

    def test_romanized_russian_needs_claude_and_says_so(self):
        self.song()
        self.lyrics(self.ROMANIZED_RUSSIAN)
        self.translator("ru")
        out = self.run_tool()
        self.assertIn("only Claude", out)
        self.assertEqual(list(self.lib.glob("*.html")), [])

    def test_romanized_russian_is_translated_when_claude_is_available(self):
        self.song()
        self.lyrics(self.ROMANIZED_RUSSIAN)
        self.translator("ru", names=("google", "claude"))
        self.run_tool()
        self.assertEqual(self.sources, ["ru-romanized"])
        self.assertIn("EN:ya tebya lyublyu", (self.lib / "Artist - Title.lrc").read_text())

    def test_translation_being_down_still_saves_the_original_and_romanization_and_english_follows(self):
        song = self.song()
        self.lyrics(self.KOREAN)
        self.translator("ko", fail=TranslateError("google: busy"))
        out = self.run_tool()
        page = self.lib / "Artist - Title.html"
        self.assertIn("annyeong", page.read_text())
        self.assertNotIn("EN:", page.read_text())
        self.assertIn("English still to come", out)
        self.assertFalse(find_lyrics.has_lyrics(song, ["lrc", "html"], find_lyrics.load_checked()))
        self.translator("ko")   # the services are back: the same command finishes the job, replacing our own files
        self.run_tool()
        self.assertIn("EN:안녕", page.read_text())
        self.assertIn("EN:안녕", (self.lib / "Artist - Title.lrc").read_text())
        self.assertTrue(find_lyrics.has_lyrics(song, ["lrc", "html"], find_lyrics.load_checked()))

    def test_with_no_romanization_a_failed_translation_saves_nothing(self):
        self.song()
        self.lyrics([Line(1.0, "Te quiero mucho mi amor"), Line(3.0, "No puedo vivir sin ti")])
        self.translator("es", fail=TranslateError("google: busy"))
        out = self.run_tool()
        self.assertIn("couldn't translate", out)
        self.assertEqual([p.name for p in self.lib.iterdir()], ["Artist - Title.mp3"])

    def test_not_found_is_remembered_and_force_asks_again(self):
        self.song()
        self.lyrics([], error=lyrics_fetch.NotFound("no lyrics found on lrclib.net"))
        self.translator("ko")
        self.run_tool()
        self.assertEqual(self.fetches, 1)
        self.run_tool()
        self.assertEqual(self.fetches, 1)          # remembered: lrclib isn't asked again
        self.run_tool("--force")
        self.assertEqual(self.fetches, 2)

    def test_report_flag_saves_the_failed_list(self):
        song = self.song()
        self.lyrics([], error=lyrics_fetch.NotFound("no lyrics found on lrclib.net"))
        self.translator("ko")
        report = self.dir / "missing.txt"
        self.run_tool("--report", str(report))
        text = report.read_text(encoding="utf-8")
        self.assertIn(str(song), text)
        self.assertIn("no lyrics found on lrclib.net", text)

    def test_an_unreachable_lrclib_is_never_remembered_as_not_found(self):
        self.song()
        self.lyrics([], error=lyrics_fetch.LyricsError("can't reach lrclib.net"))
        self.translator("ko")
        self.run_tool()
        self.run_tool()
        self.assertEqual(self.fetches, 2)

    def test_switching_layers_needs_no_lookups_and_rewrites_only_our_files(self):
        self.song()
        self.lyrics(self.KOREAN)
        self.translator("ko")
        self.run_tool("--formats", "lrc,txt,html")
        theirs = self.lib / "notes.txt"
        theirs.write_text("my notes")

        def boom(*a, **k):
            raise AssertionError("no lookups allowed")

        class NoLookups:
            skipped, names = {}, []

            def __init__(self, *a, **k):
                pass
            detect = translate = boom
        find_lyrics.fetch = boom
        find_lyrics.Translator = NoLookups
        self.run_tool("--relayer", "--layers", "original,english", "--formats", "lrc,txt,html")
        lrc = (self.lib / "Artist - Title.lrc").read_text()
        self.assertIn("EN:안녕", lrc)
        self.assertNotIn("annyeong", lrc)
        self.assertNotIn("annyeong", (self.lib / "Artist - Title.txt").read_text())
        self.assertEqual(theirs.read_text(), "my notes")

    def test_a_folder_that_cant_be_written_is_reported_and_leaves_nothing_behind(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        self.song()
        self.lyrics(self.KOREAN)
        self.translator("ko")
        self.lib.chmod(0o555)
        self.addCleanup(self.lib.chmod, 0o755)
        out = self.run_tool()
        self.assertIn("error", out)
        self.assertEqual([p.name for p in self.lib.iterdir()], ["Artist - Title.mp3"])

    def test_songs_with_dots_in_their_names_are_recognised_as_done(self):
        for name in ("Fred again.. - Title.mp3", "Mr. Brightside.mp3", "Ch. hololive - Song.mp3"):
            with self.subTest(name=name):
                song = self.song(name)
                for ext in (".lrc", ".html"):
                    (self.lib / (song.stem + ext)).write_text("x")
                self.assertTrue(find_lyrics.has_lyrics(song, ["lrc", "html"]))
                self.assertEqual({p.name for p in find_lyrics.existing_outputs(song, ["lrc", "html"]).values()},
                                 {song.stem + ".lrc", song.stem + ".html"})

    def test_a_missing_folder_says_so_plainly(self):
        out = self.run_tool(str(self.dir / "nope"))
        self.assertIn("doesn't exist", out)

    def test_folder_problem_explains_a_macos_privacy_block(self):
        self.assertEqual(common.folder_problem(self.lib), "")
        self.assertIn("isn't connected", common.folder_problem("/Volumes/NoSuchDrive-xyz/Music"))
        if os.geteuid() != 0:
            self.lib.chmod(0o000)
            self.addCleanup(self.lib.chmod, 0o755)
            self.assertIn("Removable Volumes", common.folder_problem(self.lib))


if __name__ == "__main__":
    unittest.main()
