import io
import json
import shutil
import subprocess
import importlib.util
import sys
import tempfile
import time
import types
import unittest
import unicodedata
from pathlib import Path

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import find_artist_art as artists  # noqa: E402
import find_lyrics  # noqa: E402
import fix_album_art as art  # noqa: E402
import fix_misidentified_tags as tags  # noqa: E402
import lyrics_fetch  # noqa: E402
import lyrics_romanize  # noqa: E402
from lyrics_render import Line, render_html, render_lrc, render_txt  # noqa: E402
from lyrics_translate import Translation  # noqa: E402
from PIL import Image  # noqa: E402


class MatchingTests(unittest.TestCase):
    def test_exact_title_is_full_match(self):
        self.assertEqual(art.match_score("The Weeknd Blinding Lights", "The Weeknd - Blinding Lights"), 1.0)

    def test_extra_words_are_not_full_match(self):
        score = art.match_score("aerospace engineering", "How Elon Musk Learned Aerospace Engineering without a degree?")
        self.assertLess(score, 1.0)

    def test_ytdlp_lookalikes_are_undone(self):
        q = art.clean_text("Sanctuary OS (Breaks⧸Jungle) ｜ Mix？")
        self.assertEqual(q, "Sanctuary OS (Breaks/Jungle) | Mix?")

    def test_macos_decomposed_names_match(self):
        nfd = unicodedata.normalize("NFD", "JENNIE 제니 'Mantra (House Remix)'")
        self.assertEqual(art.match_score(art.clean_text(nfd), "JENNIE 제니 'Mantra (House Remix)'"), 1.0)

    def test_empty_inputs(self):
        self.assertEqual(art.match_score("", "x"), 0.0)
        self.assertEqual(art.match_score("x", ""), 0.0)


class SearchQueryTests(unittest.TestCase):
    def test_filename_searched_when_tags_lack_artist(self):
        qs = art.search_queries(Path("Unlike Pluto - Guts (Lyrics).opus"), {"title": "Guts (Lyrics)", "artist": "", "text": ""})
        self.assertEqual(qs[0], "Unlike Pluto - Guts")

    def test_filename_always_tried_after_tags(self):
        qs = art.search_queries(Path("¥ØU$UK€ (LIVE) @ DEF.flac"), {"title": "Like You", "artist": "Naomi Raine", "text": ""})
        self.assertEqual(qs[:2], ["Naomi Raine Like You", "¥ØU$UK€ (LIVE) @ DEF"])


class ArtistNameTests(unittest.TestCase):
    def test_features_and_semicolons_split_but_ampersands_stay(self):
        self.assertEqual(artists.split_artists("Daft Punk feat. Pharrell Williams"), ["Daft Punk", "Pharrell Williams"])
        self.assertEqual(artists.split_artists("A; B ft. C"), ["A", "B", "C"])
        self.assertEqual(artists.split_artists("Simon & Garfunkel"), ["Simon & Garfunkel"])
        self.assertEqual(artists.split_artists("Tyler, The Creator"), ["Tyler, The Creator"])

    def test_placeholders_are_not_artists(self):
        for name in ("Various Artists", "unknown", "VA", "", "x" * 200):
            self.assertEqual(artists.split_artists(name), [], name)

    def test_combinations_can_be_split_for_lookup(self):
        self.assertEqual(artists.name_parts("Fred again.. & Thomas Bangalter"), ["Fred again..", "Thomas Bangalter"])
        self.assertEqual(artists.name_parts("Skrillex x Fred again"), ["Skrillex", "Fred again"])
        self.assertEqual(artists.name_parts("Radiohead"), [])

    def test_same_artist_different_spelling_has_one_key(self):
        self.assertEqual(artists.name_key("Fred again.."), artists.name_key("fred AGAIN"))

    def test_filenames_are_safe(self):
        self.assertEqual(artists.safe_filename("AC/DC"), "AC_DC")
        self.assertEqual(artists.safe_filename(".hidden"), "hidden")
        self.assertEqual(artists.safe_filename("Sanctuary OS ｜ Mix？"), "Sanctuary OS _ Mix_")
        self.assertEqual(artists.safe_filename("///"), "___")
        self.assertEqual(artists.safe_filename(""), "artist")

    def test_deezer_placeholder_pictures_are_ignored(self):
        real = "https://cdn-images.dzcdn.net/images/artist/0d58cfbc90f2e776608bcdc0c45a4711/1000x1000-000000-80-0-0.jpg"
        empty = "https://cdn-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"
        self.assertEqual(artists.picture_of({"picture_xl": empty, "picture_big": real}), real)
        self.assertEqual(artists.picture_of({"picture_xl": empty}), "")


class ArtistLookupTests(unittest.TestCase):
    """Deezer and YouTube are replaced by canned answers so these run offline."""
    PIC = "https://cdn-images.dzcdn.net/images/artist/%s/1000x1000-000000-80-0-0.jpg"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        buf = io.BytesIO()
        Image.new("RGB", (400, 400), "blue").save(buf, "JPEG")
        self.jpeg = buf.getvalue()
        self.catalog = {}     # search text -> list of Deezer artists
        self.yt_catalog = {}  # artist name -> list of YouTube channels (empty = Deezer's word is final)
        self.old = (artists.deezer_get, artists.download, artists.youtube_channels)
        artists.deezer_get = lambda path, **kw: {"data": self.catalog.get(kw.get("q", ""), [])}
        artists.download = lambda url: self.jpeg
        artists.youtube_channels = lambda name: self.yt_catalog.get(name, [])
        self.addCleanup(lambda: (setattr(artists, "deezer_get", self.old[0]), setattr(artists, "download", self.old[1]),
                                 setattr(artists, "youtube_channels", self.old[2])))

    def artist(self, name, fans=1000, pic="abc"):
        return {"id": abs(hash(name + str(fans))) % 10**6, "name": name, "nb_fan": fans, "link": "https://deezer/x",
                "picture_xl": self.PIC % pic if pic else self.PIC.replace("%s", "")}

    def channel(self, name, fans=1000, pic="https://yt3.ggpht.com/x=s800-c-k-c0x00ffffff-no-rj-mo"):
        return {"name": name, "nb_fan": fans, "link": "https://youtube.com/channel/x", "picture_xl": pic}

    def resolve(self, name):
        return artists.resolve_job(artists.Job(name, 1), {"search_results": 5})

    def patch_yt_dlp(self, run):
        """Run the real youtube_channels(), with a stand-in for the yt-dlp command."""
        artists.youtube_channels = self.old[2]
        self.addCleanup(setattr, artists, "ytdlp_command", artists.ytdlp_command)
        self.addCleanup(setattr, artists.subprocess, "run", artists.subprocess.run)
        self.addCleanup(setattr, artists.YOUTUBE_GAVE_UP, "failures", 0)
        artists.ytdlp_command = lambda: ["yt-dlp"]
        artists.subprocess.run = run
        artists.YOUTUBE_GAVE_UP.failures = 0
        self.addCleanup(setattr, artists.YOUTUBE_LIMIT, "gap", artists.YOUTUBE_LIMIT.gap)
        artists.YOUTUBE_LIMIT.gap, artists.YOUTUBE_LIMIT.next = 0.0, 0.0  # don't make the tests wait

    def yt_dlp_run(self, entries):
        """(the yt-dlp command line, the channels parsed out of its output)."""
        seen = []

        def fake(argv, **kw):
            seen.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"entries": entries}), stderr="")

        self.patch_yt_dlp(fake)
        out = artists.youtube_channels("Anyone")
        return seen[0], out

    def test_exact_name_picks_the_most_popular(self):
        self.catalog["Sleep"] = [self.artist("Sleep", 10), self.artist("SLEEP", 9000), self.artist("Sleepy", 99999)]
        job = self.resolve("Sleep")
        self.assertEqual(job.status, "ready")
        self.assertEqual(job.picks[0].fans, 9000)

    def test_similar_name_is_offered_not_used(self):
        self.catalog["JENNIE 제니"] = [self.artist("JENNIE", 500), self.artist("Unrelated Band", 900)]
        job = self.resolve("JENNIE 제니")
        self.assertEqual(job.status, "unsure")
        self.assertEqual([a["name"] for a in job.candidates], ["JENNIE"])

    def test_nothing_close_is_a_failure(self):
        self.catalog["Zzqxv"] = [self.artist("Something Else")]
        job = self.resolve("Zzqxv")
        self.assertEqual(job.status, "failed")
        self.assertIn("no artist", job.reason)

    def test_artist_without_a_picture_says_so(self):
        self.catalog["Ghost"] = [self.artist("Ghost", pic=None)]
        job = self.resolve("Ghost")
        self.assertEqual(job.status, "failed")
        self.assertIn("no picture", job.reason)

    def test_youtube_fills_in_when_deezer_doesnt_know_the_artist(self):
        self.yt_catalog["Some YouTuber"] = [self.channel("Some YouTuber", 4000)]
        job = self.resolve("Some YouTuber")
        self.assertEqual(job.status, "ready")
        self.assertEqual(job.picks[0].deezer_name, "Some YouTuber")
        self.assertEqual(job.picks[0].link, "https://youtube.com/channel/x")

    def test_youtube_is_only_tried_when_deezer_comes_up_empty(self):
        self.catalog["Sleep"] = [self.artist("Sleep", 10)]
        called = []
        artists.youtube_channels = lambda name: called.append(name) or []
        job = self.resolve("Sleep")
        self.assertEqual(job.status, "ready")
        self.assertEqual(called, [])  # Deezer already had an exact match; no need to ask YouTube

    def test_avatar_url_is_upsized(self):
        self.assertEqual(artists.big_avatar([{"url": "//yt3.ggpht.com/x=s88-c-k-c0x00ffffff-no-rj-mo", "width": 88},
                                             {"url": "https://yt3.ggpht.com/x=s176-c-k-c0x00ffffff-no-rj-mo", "width": 176}]),
                         "https://yt3.ggpht.com/x=s800-c-k-c0x00ffffff-no-rj-mo")
        self.assertEqual(artists.big_avatar([]), "")

    def test_youtube_channels_returns_nothing_without_yt_dlp(self):
        self.addCleanup(setattr, artists, "ytdlp_command", artists.ytdlp_command)
        artists.ytdlp_command = lambda: None
        self.assertEqual(artists.youtube_channels("Anyone"), [])

    def test_combination_is_looked_up_as_its_parts(self):
        self.catalog["A & B"] = [self.artist("Unrelated")]
        self.catalog["A"] = [self.artist("A", 5)]
        self.catalog["B"] = [self.artist("B", 6)]
        job = self.resolve("A & B")
        self.assertEqual(job.status, "ready")
        self.assertEqual([p.name for p in job.picks], ["A", "B"])

    def test_only_one_page_of_youtube_results_is_asked_for(self):
        # Without a limit yt-dlp pages through every result YouTube has, which takes
        # tens of seconds per artist and gets us refused (HTTP 403).
        self.assertIn("--playlist-end", self.yt_dlp_run(entries=[])[0])

    def test_youtube_videos_are_ignored_only_channels_have_a_picture_of_the_artist(self):
        argv, out = self.yt_dlp_run(entries=[
            {"ie_key": "Youtube", "channel": "Some Uploader", "channel_url": "https://youtube.com/channel/v",
             "thumbnails": [{"url": "https://i.ytimg.com/vi/abc/hq720.jpg", "width": 720}]},
            {"ie_key": "YoutubeTab", "channel": "Real Artist - Topic", "channel_url": "https://youtube.com/channel/c",
             "channel_follower_count": 1234,
             "thumbnails": [{"url": "https://yt3.ggpht.com/x=s176-c-k-c0x00ffffff-no-rj-mo", "width": 176}]},
        ])
        self.assertEqual([c["name"] for c in out], ["Real Artist"])
        self.assertEqual(out[0]["nb_fan"], 1234)

    def test_youtube_is_dropped_once_it_keeps_refusing_us(self):
        # Every refusal costs seconds of yt-dlp retries, so stop asking rather than
        # pay that for every remaining artist.
        calls = []

        def refuse(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP Error 403: Forbidden")

        self.patch_yt_dlp(refuse)
        for i in range(artists.YOUTUBE_GIVE_UP_AFTER + 3):
            artists.youtube_channels(f"Artist {i}")
        self.assertEqual(len(calls), artists.YOUTUBE_GIVE_UP_AFTER)
        self.assertTrue(artists.YOUTUBE_GAVE_UP.given_up())

    def test_a_working_search_clears_the_earlier_refusals(self):
        results = [1, 1, 0]  # refused, refused, then fine

        def flaky(argv, **kw):
            rc = results.pop(0)
            return types.SimpleNamespace(returncode=rc, stdout="" if rc else json.dumps({"entries": []}), stderr="")

        self.patch_yt_dlp(flaky)
        for name in ("A", "B", "C"):
            artists.youtube_channels(name)
        self.assertEqual(artists.YOUTUBE_GAVE_UP.failures, 0)

    def test_the_bracketless_query_is_skipped_once_a_name_matches_exactly(self):
        # "Name (Band)" is searched as tagged first; only a miss is worth a second request.
        asked = []
        self.catalog["Tricot (トリコ)"] = [self.artist("Tricot (トリコ)")]
        real = artists.deezer_get
        artists.deezer_get = lambda path, **kw: (asked.append(kw.get("q")), real(path, **kw))[1]
        self.addCleanup(setattr, artists, "deezer_get", real)
        artists.search_artists("Tricot (トリコ)")
        self.assertEqual(asked, ["Tricot (トリコ)"])
        artists.search_artists("Nobody (Here)")
        self.assertEqual(asked[1:], ["Nobody (Here)", "Nobody", "Here"])

    def test_broken_download_is_a_failure_not_a_crash(self):
        self.catalog["Radiohead"] = [self.artist("Radiohead")]
        artists.download = lambda url: b"<html>not an image</html>"
        job = self.resolve("Radiohead")
        self.assertEqual(job.status, "failed")

    def test_full_run_saves_pictures_and_skips_them_next_time(self):
        import os
        self.catalog["Radiohead"] = [self.artist("Radiohead")]
        library = self.dir / "lib"
        library.mkdir()
        (library / "Radiohead - Creep.mp3").write_bytes(b"")
        config = self.dir / "c.toml"
        config.write_text(f'music_dir = {json.dumps(str(library))}\nsave_logs = false\n', encoding="utf-8")
        out = library / "Artist Art" / "Radiohead.jpg"

        def run(*argv):
            old = sys.argv
            sys.argv = ["find_artist_art.py", "--config", str(config), "--no-progress", *argv]
            try:
                artists.main()
            finally:
                sys.argv = old

        run("--dry-run")
        self.assertFalse(out.exists())
        run("--auto")
        self.assertTrue(out.is_file())
        self.assertEqual(Image.open(out).size, (400, 400))
        out.write_bytes(b"mine")
        run("--auto")
        self.assertEqual(out.read_bytes(), b"mine")  # never replaced without --force
        run("--auto", "--force")
        self.assertNotEqual(out.read_bytes(), b"mine")

    def test_many_artists_settle_with_the_right_outcomes(self):
        import threading
        self.catalog.update({"Radiohead": [self.artist("Radiohead")],
                             "JENNIE 제니": [self.artist("JENNIE")],
                             "A & B": [], "A": [self.artist("A")], "B": [self.artist("B")]})
        real = artists.download
        artists.download = lambda url: (time.sleep(0.02), real(url))[1]  # slow downloads
        jobs = ([artists.Job(f"Artist {i}") for i in range(30)]
                + [artists.Job(n) for n in ("Radiohead", "JENNIE 제니", "A & B")])
        for j in jobs[:30]:
            self.catalog[j.name] = [self.artist(j.name)]
        done = threading.Event()
        threading.Thread(target=lambda: (artists.find_pictures(jobs, {"search_results": 5}, 4, True), done.set()),
                         daemon=True).start()
        self.assertTrue(done.wait(20), "find_pictures hung")
        self.assertTrue(all(j.status == "ready" for j in jobs[:30]))
        self.assertEqual([j.status for j in jobs[30:]], ["ready", "unsure", "ready"])
        self.assertEqual([p.name for p in jobs[32].picks], ["A", "B"])
        self.assertTrue(all(p.image for j in jobs for p in j.picks))

    def test_a_crashing_lookup_does_not_hang_the_others(self):
        import threading
        self.catalog["Fine"] = [self.artist("Fine")]
        old = artists.search_artists
        artists.search_artists = lambda name: 1 / 0 if name == "Boom" else old(name)
        self.addCleanup(setattr, artists, "search_artists", old)
        jobs = [artists.Job("Boom"), artists.Job("Fine")]
        done = threading.Event()
        threading.Thread(target=lambda: (artists.find_pictures(jobs, {"search_results": 5}, 2, True), done.set()),
                         daemon=True).start()
        self.assertTrue(done.wait(20), "find_pictures hung")
        self.assertEqual([j.status for j in jobs], ["failed", "ready"])

    def test_own_image_file_can_be_used(self):
        image = self.dir / "pic.png"
        Image.new("RGB", (300, 300), "green").save(image)
        pick = artists.pick_from_text("Some DJ", str(image))
        self.assertEqual(Image.open(io.BytesIO(pick.image)).format, "JPEG")
        with self.assertRaises(ValueError):
            artists.pick_from_text("Some DJ", "definitely not a file")


class MenuTests(unittest.TestCase):
    def test_dragged_and_quoted_paths(self):
        import interactive
        self.assertEqual(interactive.clean_path("/Volumes/My\\ Drive/Music\\ Mix "), "/Volumes/My Drive/Music Mix")
        self.assertEqual(interactive.clean_path("'/Volumes/My Drive/Mix'"), "/Volumes/My Drive/Mix")
        self.assertEqual(interactive.clean_path("  "), "")

    def test_log_file_is_written(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        out = subprocess.run([sys.executable, "-c", (
            "import common, pathlib; common.LOG_DIR = pathlib.Path(%r); "
            "common.start_log('test', {'save_logs': True}); print('hello log')") % str(tmp)],
            cwd=ROOT, capture_output=True, text=True)
        logs = list(tmp.glob("*_test.log"))
        self.assertEqual(len(logs), 1, out.stderr)
        self.assertIn("hello log", logs[0].read_text(encoding="utf-8"))


class FolderMenuTests(unittest.TestCase):
    """The Folders screen, driven through its numbered-menu fallback (no real terminal needed)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.lib = self.dir / "lib"
        (self.lib / "Mixes").mkdir(parents=True)
        self.config = self.dir / "c.toml"
        self.config.write_text(f'music_dir = {json.dumps(str(self.lib))}\n', encoding="utf-8")

    def run_menu(self, method, *inputs):
        import common as common_mod
        import interactive
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("\n".join(inputs) + "\n")
        sys.stdout = io.StringIO()
        try:
            app = interactive.App(str(self.config))
            getattr(app, method)()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        return common_mod.read_raw_config(self.config)

    def test_lyrics_folders_can_be_added_via_the_menu(self):
        # 4 = Lyrics folders, 1 = Add a folder, then the path, 4 = back, 6 = back
        cfg = self.run_menu("folders", "4", "1", "Mixes", "4", "6")
        self.assertEqual(cfg["lyrics"]["folders"], [".", "Mixes"])

    def test_artist_art_folders_can_be_replaced_via_the_menu(self):
        # 3 = Artist picture folders, 3 = Use only one folder, then the path, 4 = back, 6 = back
        cfg = self.run_menu("folders", "3", "3", "Mixes", "4", "6")
        self.assertEqual(cfg["artist_art"]["folders"], ["Mixes"])

    def test_album_art_folders_are_unaffected_by_editing_other_sections(self):
        cfg = self.run_menu("folders", "4", "1", "Mixes", "4", "6")
        self.assertEqual(cfg["album_art"]["folders"], ["."])

    def test_duplicates_folders_can_be_added_via_the_menu(self):
        # 5 = Duplicate-check folders, 1 = Add a folder, then the path, 4 = back, 6 = back
        cfg = self.run_menu("folders", "5", "1", "Mixes", "4", "6")
        self.assertEqual(cfg["duplicates"]["folders"], [".", "Mixes"])


class ParseTests(unittest.TestCase):
    def test_artist_dash_title(self):
        p = tags.parse_filename("Fred again.. - Jungle")
        self.assertEqual((p["artist"], p["title"]), ("Fred again..", "Jungle"))

    def test_track_number_goes_to_track_tag(self):
        p = tags.parse_filename("03 - Kyle")
        prop = tags.determine_proposed_tags(p, "Album")
        self.assertEqual((prop["title"], prop["track"]), ("Kyle", "3"))

    def test_live_set_keeps_full_name(self):
        stem = "Fred again.. & Thomas Bangalter (USB002, Alexandra Palace)"
        prop = tags.determine_proposed_tags(tags.parse_filename(stem), "USB002")
        self.assertEqual(prop["title"], stem)

    def test_remix_is_title_not_artist(self):
        p = tags.parse_filename("solo (KETTAMA remix)")
        self.assertIsNone(p["artist"])

    def test_severe_mismatch(self):
        self.assertTrue(tags.is_severe_mismatch("Boiler Room London DJ Set", "Delilah (pull me out of this)"))
        self.assertFalse(tags.is_severe_mismatch("Novacane", "Novocaine"))
        self.assertFalse(tags.is_severe_mismatch("サニーボーイ", "Sunny Boy"))


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            return common.load_config(f.name)
        finally:
            Path(f.name).unlink()

    def test_defaults_fill_missing_keys(self):
        cfg = self.load('music_dir = "/tmp"\n')
        self.assertEqual(cfg["album_art"]["min_match"], 1.0)
        self.assertEqual(cfg["tags"]["title_mode"], "auto")

    def test_bad_values_exit_cleanly(self):
        for bad in ('workers = "lots"', "workers = 0", '[tags]\ntitle_mode = "nope"',
                    "[album_art]\nmin_match = 5", "this is not toml ["):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.load(bad)

    def test_artist_art_settings_are_checked(self):
        self.assertEqual(self.load('music_dir = "/tmp"\n')["artist_art"]["output_dir"], "Artist Art")
        for bad in ('[artist_art]\noutput_dir = " "', "[artist_art]\nsearch_results = 0", "[artist_art]\nfolders = [1]"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.load(bad)

    def test_missing_explicit_config_exits(self):
        with self.assertRaises(SystemExit):
            common.load_config("/nonexistent/config.toml")


class CommandTests(unittest.TestCase):
    """The music-tools command must only ever write to the config it was pointed at."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.config = self.dir / "new.toml"  # doesn't exist yet
        (self.dir / "Mixes").mkdir()

    def run_cmd(self, *args):
        env = {**__import__("os").environ, "MUSIC_TOOLS_CONFIG": str(self.config)}
        return subprocess.run([sys.executable, str(ROOT / "music-tools"), *args],
                              capture_output=True, text=True, env=env)

    def test_env_config_used_even_if_missing(self):
        import os
        old = os.environ.get("MUSIC_TOOLS_CONFIG")
        os.environ["MUSIC_TOOLS_CONFIG"] = str(self.config)
        try:
            self.assertEqual(common.config_path(), self.config)
        finally:
            if old is None:
                del os.environ["MUSIC_TOOLS_CONFIG"]
            else:
                os.environ["MUSIC_TOOLS_CONFIG"] = old

    def test_set_folders_and_values(self):
        self.assertEqual(self.run_cmd("dir", str(self.dir)).returncode, 0)
        self.assertEqual(self.run_cmd("folders", "use", "Mixes").returncode, 0)
        self.assertEqual(self.run_cmd("set", "album_art.min_match", "0.9").returncode, 0)
        cfg = common.load_config(str(self.config))
        self.assertEqual(cfg["album_art"]["folders"], ["Mixes"])
        self.assertEqual(cfg["album_art"]["min_match"], 0.9)
        self.assertEqual(cfg["music_dir"], str(self.dir.resolve()))

    def test_bad_values_are_rejected_and_not_saved(self):
        self.run_cmd("set", "workers", "4")
        before = self.config.read_text(encoding="utf-8")
        for args in (("set", "workers", "lots"), ("set", "tags.title_mode", "nope"), ("set", "bogus", "1")):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_cmd(*args).returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_help_lists_all_tools(self):
        out = self.run_cmd("help").stdout
        self.assertIn("ALBUM ART", out)
        self.assertIn("ARTIST PICTURES", out)
        self.assertIn("TAGS", out)

    def test_artists_command_and_setting(self):
        self.assertEqual(self.run_cmd("set", "artist_art.output_dir", "Faces").returncode, 0)
        self.assertEqual(common.load_config(str(self.config))["artist_art"]["output_dir"], "Faces")
        self.assertIn("--list-missing", self.run_cmd("help", "artists").stdout)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed to make test audio")
class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), "red").save(buf, "JPEG")
        self.jpeg = art.to_jpeg(buf.getvalue(), crop=True, quality=90)

    def make(self, name, **meta):
        path = self.dir / name
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc", "-t", "0.2"]
        for k, v in meta.items():
            cmd += ["-metadata", f"{k}={v}"]
        subprocess.run(cmd + [str(path)], check=True)
        return path

    def test_embed_art_every_format(self):
        for ext in ("mp3", "m4a", "flac", "ogg", "opus"):
            with self.subTest(ext=ext):
                p = self.make(f"song.{ext}")
                self.assertFalse(art.has_art(p))
                art.embed(p, self.jpeg)
                self.assertTrue(art.has_art(p))

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed")
    def test_convert_webm_keeps_audio(self):
        src = self.dir / "clip.webm"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2",
                            "-c:a", "libopus", str(src)])
        if r.returncode:
            self.skipTest("ffmpeg without libopus")
        art.move_to_trash = lambda p: False  # don't touch the real Trash in tests
        new, msg = art.convert_webm(src)
        self.assertEqual(new, src.with_suffix(".opus"), msg)
        self.assertTrue(src.exists())
        art.embed(new, self.jpeg)
        self.assertTrue(art.has_art(new))
        self.assertIsNone(art.convert_webm(src)[0])  # won't overwrite the .opus

    def test_artists_come_from_tags_then_filename(self):
        for ext in ("mp3", "m4a", "flac", "opus"):
            with self.subTest(ext=ext):
                p = self.make(f"song.{ext}", artist="Daft Punk feat. Pharrell Williams")
                self.assertEqual(artists.artists_of(p), ["Daft Punk", "Pharrell Williams"])
        self.assertEqual(artists.artists_of(self.make("Kendrick Lamar - Humble.m4a")), ["Kendrick Lamar"])
        self.assertEqual(artists.artists_of(self.make("just a title.mp3")), [])
        self.assertEqual(artists.artists_of(self.dir / "missing.mp3"), [])

    def test_crop_is_square(self):
        self.assertEqual(Image.open(io.BytesIO(self.jpeg)).size, (360, 360))

    def test_corrupt_file_does_not_crash(self):
        p = self.dir / "broken.flac"
        p.write_bytes(b"not audio at all")
        self.assertFalse(art.has_art(p))
        opts = {**common.DEFAULTS["tags"], "dry_run": False, "only_severe": False, "only_mismatched": False}
        res = tags.process_audio_file(p, opts)
        self.assertTrue(res is None or res.get("error"))

    def test_fix_tags_every_format(self):
        opts = {**common.DEFAULTS["tags"], "dry_run": False, "only_severe": False, "only_mismatched": False}
        for ext in ("mp3", "m4a", "flac"):
            with self.subTest(ext=ext):
                p = self.make(f"Artist - Title.{ext}", title="Wrong", album="Wrong Album", MUSICBRAINZ_TRACKID="x")
                res = tags.process_audio_file(p, opts)
                self.assertFalse(res.get("error"), res["lines"])
                cur = tags.get_current_tags(tags.mutagen.File(str(p)))
                self.assertEqual((cur["title"], cur["artist"]), ("Title", "Artist"))
                self.assertEqual(cur["picard_keys"], [])

    def test_set_artist_names_the_artist_a_filename_cannot(self):
        opts = {**common.DEFAULTS["tags"], "dry_run": False, "only_severe": False, "only_mismatched": False,
                "set_artist": "Rad Cat"}
        p = self.make("what u want!.mp3", title="what u want!")
        self.assertFalse(tags.process_audio_file(p, opts).get("error"))
        cur = tags.get_current_tags(tags.mutagen.File(str(p)))
        self.assertEqual((cur["title"], cur["artist"]), ("what u want!", "Rad Cat"))


class LyricsFetchTests(unittest.TestCase):
    def setUp(self):
        self.old = lyrics_fetch.api_get
        self.addCleanup(setattr, lyrics_fetch, "api_get", self.old)

    def test_parse_lrc_handles_repeated_stamps_and_sorts(self):
        lrc = "[00:01.00]Hello\n[00:03.50][00:10.00]Repeat\n\n[00:05.00]Gap above\n"
        lines = lyrics_fetch.parse_lrc(lrc)
        self.assertEqual([(l.time, l.text) for l in lines],
                         [(1.0, "Hello"), (3.5, "Repeat"), (5.0, "Gap above"), (10.0, "Repeat")])

    def test_parse_plain_trims_and_collapses_verse_breaks(self):
        text = "\n\nFirst\nSecond\n\n\nThird\n\n"
        lines = lyrics_fetch.parse_plain(text)
        self.assertEqual([(l.time, l.text) for l in lines],
                         [(None, "First"), (None, "Second"), (None, ""), (None, "Third")])

    def test_fetch_prefers_an_exact_match_over_a_search(self):
        calls = []

        def fake_get(path, **params):
            calls.append(path)
            if path == "get":
                return {"artistName": "Radiohead", "trackName": "Creep", "syncedLyrics": "[00:00.00]la la"}
            return [{"artistName": "Someone Else", "trackName": "Creep", "plainLyrics": "nope"}]

        lyrics_fetch.api_get = fake_get
        result = lyrics_fetch.fetch("Radiohead", "Creep")
        self.assertEqual(calls, ["get"])
        self.assertTrue(result.synced)
        self.assertEqual(result.lines[0].text, "la la")

    def test_fetch_falls_back_to_search_and_ignores_instrumentals(self):
        def fake_get(path, **params):
            if path == "get":
                return None
            return [{"artistName": "Wrong Artist", "trackName": "Creep", "plainLyrics": "wrong"},
                    {"artistName": "Radiohead", "trackName": "Creep (Karaoke)", "instrumental": True, "plainLyrics": "x"},
                    {"artistName": "Radiohead", "trackName": "Creep", "plainLyrics": "right"}]

        lyrics_fetch.api_get = fake_get
        result = lyrics_fetch.fetch("Radiohead", "Creep")
        self.assertEqual(result.lines[0].text, "right")

    def test_fetch_raises_when_nothing_is_found(self):
        lyrics_fetch.api_get = lambda path, **kw: None if path == "get" else []
        with self.assertRaises(lyrics_fetch.LyricsError):
            lyrics_fetch.fetch("Nobody", "Nothing")


class LyricsRomanizeTests(unittest.TestCase):
    def test_korean_is_romanized_without_any_package(self):
        self.assertEqual(lyrics_romanize.romanize_korean("안녕하세요"), "annyeonghaseyo")
        self.assertEqual(lyrics_romanize.romanize_korean("사랑해, Baby!"), "saranghae, Baby!")
        self.assertTrue(lyrics_romanize.available("ko"))

    def test_romanize_fills_in_only_supported_languages(self):
        lines = [Line(None, "안녕"), Line(None, "")]
        self.assertTrue(lyrics_romanize.romanize(lines, "ko"))
        self.assertEqual(lines[0].romaji, "annyeong")
        self.assertEqual(lines[1].romaji, "")

        lines = [Line(None, "hello")]
        self.assertFalse(lyrics_romanize.romanize(lines, "en"))
        self.assertEqual(lines[0].romaji, "")

        lines = [Line(None, "привет")]
        self.assertTrue(lyrics_romanize.romanize(lines, "ru"))
        self.assertEqual(lines[0].romaji, "privet")

    def test_russian_is_romanized_without_any_package(self):
        self.assertEqual(lyrics_romanize.romanize_russian("привет"), "privet")
        self.assertEqual(lyrics_romanize.romanize_russian("Привет, Baby!"), "Privet, Baby!")
        self.assertEqual(lyrics_romanize.romanize_russian("Щёлкунчик"), "Shchyolkunchik")
        self.assertEqual(lyrics_romanize.romanize_russian("семья"), "semya")  # ь dropped, not an apostrophe
        self.assertEqual(lyrics_romanize.romanize_russian("подъезд"), "podezd")  # ъ dropped too
        # ё stored as е + a combining diaeresis (some lyrics sources do this) still comes out as "yo"
        self.assertEqual(lyrics_romanize.romanize_russian("ё"), "yo")
        self.assertTrue(lyrics_romanize.available("ru"))
        self.assertEqual(lyrics_romanize.status()["ru"], "built-in")


class LyricsRenderTests(unittest.TestCase):
    def test_lrc_repeats_the_timestamp_for_each_extra_line(self):
        lines = [Line(1.5, "hi", romaji="", english="hello")]
        text = render_lrc(lines, "Title", "Artist")
        self.assertIn("[ti:Title]", text)
        self.assertEqual(text.count("[00:01.50]"), 2)

    def test_txt_stacks_original_romaji_and_english(self):
        text = render_txt([Line(None, "原文", romaji="genbun", english="original text")])
        self.assertEqual(text, "原文\ngenbun\noriginal text\n")

    def test_html_escapes_and_includes_a_player_only_when_synced_and_playable(self):
        page = render_html([Line(1.0, "<script>bad</script>", english="safe")], "Title", audio_href="song.mp3")
        self.assertNotIn("<script>bad</script>", page)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", page)
        self.assertIn("<audio", page)
        silent = render_html([Line(None, "hi")], "Title")
        self.assertNotIn("<audio", silent)


class FindLyricsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.old_fetch = find_lyrics.fetch
        self.old_translator = find_lyrics.Translator
        self.addCleanup(setattr, find_lyrics, "fetch", self.old_fetch)
        self.addCleanup(setattr, find_lyrics, "Translator", self.old_translator)
        self.addCleanup(setattr, find_lyrics, "CHECKED_FILE", find_lyrics.CHECKED_FILE)
        find_lyrics.CHECKED_FILE = self.dir / "checked.json"   # never touch the real one

    def fake_fetch(self, lines, synced=True):
        find_lyrics.fetch = lambda artist, title, album="", duration=None: lyrics_fetch.Lyrics(lines, synced, artist, title)

    def fake_translator(self, lang="ja", translated=None):
        class Fake:
            def __init__(self, *a, **k):
                pass

            def detect(self, texts):
                return lang

            def translate(self, texts, source):
                return Translation(translated or [f"[{t}]" for t in texts], lang, "fake")
        find_lyrics.Translator = Fake

    def test_query_variants_pairs_the_filenames_artist_with_the_cleaned_title(self):
        # a common mistagging: the Artist tag is really a channel/curator name ("Valiant"), and
        # the real artist ("LESST") only shows up in the filename/title. The cleaned title (no
        # "(feat. ...)") needs to be tried together with THAT artist, not just with the tag one.
        library = self.dir / "lib3"
        library.mkdir()
        song = library / "LESST - Wicked (feat. Elvya).mp3"
        song.write_bytes(b"")
        job = find_lyrics.Job(song, "Valiant", "LESST - Wicked (feat. Elvya)")
        variants = find_lyrics.query_variants(job)
        self.assertIn(("LESST", "Wicked"), variants)

    def test_process_job_romanizes_and_translates(self):
        self.fake_fetch([Line(1.0, "안녕"), Line(3.0, "사랑해")])
        self.fake_translator("ko", ["Hello", "I love you"])
        job = find_lyrics.Job(Path("song.mp3"), "Artist", "Title")
        find_lyrics.process_job(job, find_lyrics.Translator(), True)
        self.assertEqual(job.status, "found")
        self.assertEqual(job.lyrics.lines[0].romaji, "annyeong")
        self.assertEqual(job.lyrics.lines[0].english, "Hello")
        self.assertEqual(job.backend, "fake")

    def test_a_text_every_service_hands_back_unchanged_counts_as_english_not_a_failure(self):
        self.fake_fetch([Line(1.0, "Vague hope nmgeai"), Line(3.0, "Sakura rain zephyr")])

        class Unchanged:
            def __init__(self, *a, **k):
                pass

            def detect(self, texts):
                return "de"   # a wrong guess: the services then return the text untouched

            def translate(self, texts, source):
                raise find_lyrics.Untranslatable("google returned the text untranslated")
        find_lyrics.Translator = Unchanged
        job = find_lyrics.Job(Path("song.mp3"), "Artist", "Title")
        find_lyrics.process_job(job, find_lyrics.Translator(), True)
        self.assertEqual((job.status, job.certain), ("skipped", True))
        self.assertIn("nothing to translate", job.reason)

    def test_process_job_skips_translation_when_already_english(self):
        self.fake_fetch([Line(1.0, "hello there")])
        self.fake_translator("en")
        job = find_lyrics.Job(Path("song.mp3"), "Artist", "Title")
        find_lyrics.process_job(job, find_lyrics.Translator(), True)
        self.assertEqual(job.lyrics.lines[0].english, "")
        self.assertEqual(job.backend, "")

    def test_no_translate_flag_is_honored(self):
        self.fake_fetch([Line(1.0, "안녕")])
        self.fake_translator("ko", ["Hello"])
        job = find_lyrics.Job(Path("song.mp3"), "Artist", "Title")
        find_lyrics.process_job(job, find_lyrics.Translator(), False)
        self.assertEqual(job.lyrics.lines[0].english, "")
        self.assertEqual(job.lyrics.lines[0].romaji, "annyeong")  # romanization isn't translation

    def test_full_run_saves_sidecars_and_skips_them_next_time(self):
        self.fake_fetch([Line(1.0, "안녕"), Line(3.0, "")], synced=True)
        self.fake_translator("ko", ["Hello"])
        library = self.dir / "lib"
        library.mkdir()
        song = library / "Artist - Title.mp3"
        song.write_bytes(b"")
        config = self.dir / "c.toml"
        config.write_text(f'music_dir = {json.dumps(str(library))}\nsave_logs = false\n', encoding="utf-8")
        lrc, html = library / "Artist - Title.lrc", library / "Artist - Title.html"

        def run(*argv):
            old = sys.argv
            sys.argv = ["find_lyrics.py", "--config", str(config), "--no-progress", *argv]
            try:
                find_lyrics.main()
            finally:
                sys.argv = old

        run("--dry-run")
        self.assertFalse(lrc.exists())
        run()
        self.assertTrue(lrc.is_file())
        self.assertTrue(html.is_file())
        self.assertIn("[00:01.00]안녕", lrc.read_text(encoding="utf-8"))
        lrc.write_text("mine", encoding="utf-8")
        run()
        self.assertEqual(lrc.read_text(encoding="utf-8"), "mine")  # not saved again without --force
        run("--force")
        self.assertNotEqual(lrc.read_text(encoding="utf-8"), "mine")

    def test_unsynced_lyrics_skip_lrc_but_still_write_html(self):
        self.fake_fetch([Line(None, "no timing here")], synced=False)
        self.fake_translator("en")
        library = self.dir / "lib2"
        library.mkdir()
        song = library / "Artist - Title.mp3"
        song.write_bytes(b"")
        config = self.dir / "c2.toml"
        config.write_text(f'music_dir = {json.dumps(str(library))}\nsave_logs = false\n', encoding="utf-8")
        old = sys.argv
        sys.argv = ["find_lyrics.py", "--config", str(config), "--no-progress", "--include-english"]
        try:
            find_lyrics.main()
        finally:
            sys.argv = old
        self.assertFalse((library / "Artist - Title.lrc").exists())
        self.assertTrue((library / "Artist - Title.html").is_file())


if __name__ == "__main__":
    unittest.main()
