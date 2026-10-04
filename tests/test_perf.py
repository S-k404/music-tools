import importlib.util
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import library_stats  # noqa: E402
import lyrics_fetch  # noqa: E402
import lyrics_local  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Handler.hits.append(self.path)
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/new")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"x" * 5000 if self.path == "/big" else b"hello " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CountingServer(ThreadingHTTPServer):
    daemon_threads = True
    connections = 0

    def get_request(self):
        _CountingServer.connections += 1
        return super().get_request()


class FetchUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _CountingServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.hits.clear()
        _CountingServer.connections = 0
        for conn in getattr(common._connections, "pool", {}).values():
            conn.close()
        common._connections.__dict__.clear()  # a clean pool for this thread
        self.addCleanup(lambda: [c.close() for c in getattr(common._connections, "pool", {}).values()])
        patcher = mock.patch.object(common, "_via_proxy", return_value=False)  # these go straight to localhost
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_to_one_host_share_a_connection(self):
        for i in range(5):
            self.assertEqual(common.fetch_url(f"{self.base}/n{i}"), f"hello /n{i}".encode())
        self.assertEqual(len(_Handler.hits), 5)
        self.assertEqual(_CountingServer.connections, 1)

    def test_each_thread_gets_its_own_connection(self):
        common.fetch_url(f"{self.base}/a")
        def other():
            common.fetch_url(f"{self.base}/b")
            for conn in common._connections.pool.values():
                conn.close()
        t = threading.Thread(target=other)
        t.start()
        t.join()
        self.assertEqual(_CountingServer.connections, 2)

    def test_an_http_error_is_raised_like_urlopen_does(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            common.fetch_url(f"{self.base}/missing")
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(common.fetch_url(f"{self.base}/ok"), b"hello /ok")  # connection still usable

    def test_an_unreachable_host_is_a_urlerror(self):
        with self.assertRaises(urllib.error.URLError):
            common.fetch_url("http://127.0.0.1:9/", timeout=2)

    def test_redirects_are_followed(self):
        self.assertEqual(common.fetch_url(f"{self.base}/old"), b"hello /new")

    def test_max_bytes_stops_early_and_drops_the_connection(self):
        data = common.fetch_url(f"{self.base}/big", max_bytes=100)
        self.assertEqual(len(data), 101)  # one more than the limit: "it was bigger"
        self.assertEqual(common.fetch_url(f"{self.base}/ok"), b"hello /ok")  # next request still works

    def test_a_connection_the_server_closed_is_replaced_quietly(self):
        common.fetch_url(f"{self.base}/first")
        for conn in common._connections.pool.values():
            conn.sock.close()  # what an idle timeout on the server looks like
        self.assertEqual(common.fetch_url(f"{self.base}/second"), b"hello /second")


class AtomicWriteTests(unittest.TestCase):
    def test_writes_text_and_bytes_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            common.atomic_write(d / "a.txt", "héllo")
            common.atomic_write(d / "b.bin", b"\x00\x01")
            self.assertEqual((d / "a.txt").read_text(encoding="utf-8"), "héllo")
            self.assertEqual((d / "b.bin").read_bytes(), b"\x00\x01")
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["a.txt", "b.bin"])

    def test_a_failed_write_cleans_up_and_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target = d / "a.txt"
            target.write_text("old")
            with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    common.atomic_write(target, "new")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual([p.name for p in d.iterdir()], ["a.txt"])


class LrclibCacheTests(unittest.TestCase):
    def setUp(self):
        lyrics_fetch._cache.clear()
        self.addCleanup(lyrics_fetch._cache.clear)

    def test_the_same_question_is_asked_once(self):
        with mock.patch.object(lyrics_fetch, "request_json", return_value=[{"trackName": "x"}]) as rj:
            a = lyrics_fetch.api_get("search", track_name="Song", artist_name="")
            b = lyrics_fetch.api_get("search", artist_name=None, track_name="Song")  # same query, other order
            self.assertEqual(a, b)
            self.assertEqual(rj.call_count, 1)
            lyrics_fetch.api_get("search", track_name="Other")
            self.assertEqual(rj.call_count, 2)

    def test_a_404_is_remembered_too_but_an_error_is_not(self):
        with mock.patch.object(lyrics_fetch, "request_json", return_value=None) as rj:
            self.assertIsNone(lyrics_fetch.api_get("get", track_name="Nope"))
            self.assertIsNone(lyrics_fetch.api_get("get", track_name="Nope"))
            self.assertEqual(rj.call_count, 1)
        err = lyrics_fetch.NetError("busy")
        with mock.patch.object(lyrics_fetch, "request_json", side_effect=err) as rj:
            for _ in range(2):
                with self.assertRaises(lyrics_fetch.LyricsError):
                    lyrics_fetch.api_get("get", track_name="Err")
            self.assertEqual(rj.call_count, 2)

    def test_old_answers_expire(self):
        with mock.patch.object(lyrics_fetch, "request_json", return_value=[]) as rj:
            lyrics_fetch.api_get("search", track_name="Song")
            with mock.patch.object(lyrics_fetch.time, "monotonic", return_value=1e9):
                lyrics_fetch.api_get("search", track_name="Song")
            self.assertEqual(rj.call_count, 2)


class LibraryWalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "Music"
        for rel in ("YouTube/a.mp3", "YouTube/sub/b.flac", "YouTube/notes.txt", "Mixes/c.m4a", "d.mp3", "Mixes/.hidden.mp3"):
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"")
        self.outside = Path(self.tmp.name).resolve() / "Elsewhere"
        self.outside.mkdir()
        (self.outside / "e.mp3").write_bytes(b"")

    def same_as_direct(self, library, folders, exts):
        self.assertEqual(library.files(folders, exts), list(common.find_audio(folders, exts)))

    def test_matches_walking_each_folder_directly(self):
        lib = library_stats.Library(self.root)
        exts = {".mp3", ".flac", ".m4a"}
        self.same_as_direct(lib, [self.root / "YouTube"], exts)
        self.same_as_direct(lib, [self.root / "YouTube", self.root / "Mixes"], exts)
        self.same_as_direct(lib, [self.root / "YouTube", self.root / "YouTube"], exts)  # overlap is counted twice, as before
        self.same_as_direct(lib, [self.root], {".mp3"})
        self.same_as_direct(lib, [self.outside], exts)  # outside the library: walked on its own
        self.same_as_direct(lib, [self.root / "YouTube", self.outside], exts)

    def test_the_library_is_walked_once_however_many_sections_ask(self):
        lib = library_stats.Library(self.root)
        with mock.patch.object(library_stats, "find_audio", wraps=common.find_audio) as walk:
            lib.files([self.root / "YouTube"], {".mp3"})
            lib.files([self.root / "Mixes"], {".m4a"})
            lib.files([lib.root], {".flac"})
        self.assertEqual(walk.call_count, 1)

    @unittest.skipIf(sys.platform == "win32", "symlinks")
    def test_a_symlinked_folder_is_walked_directly(self):
        link = self.root / "Linked"
        link.symlink_to(self.outside, target_is_directory=True)
        lib = library_stats.Library(self.root)
        self.same_as_direct(lib, [link], {".mp3"})
        self.assertEqual([p.name for p in lib.files([link], {".mp3"})], ["e.mp3"])


class EmbeddedLyricsTests(unittest.TestCase):
    def test_a_parsed_file_is_used_instead_of_opening_the_song_again(self):
        parsed = mock.Mock(tags={"\xa9lyr": ["from the open file"]})
        with mock.patch.object(lyrics_local, "MP4", side_effect=AssertionError("reopened")), \
                mock.patch.object(lyrics_local, "MutagenFile", side_effect=AssertionError("reopened")):
            self.assertEqual(lyrics_local.embedded_lyrics(Path("x.m4a"), parsed), "from the open file")
            parsed.tags = {"lyrics": ["flac lyrics"]}
            self.assertEqual(lyrics_local.embedded_lyrics(Path("x.flac"), parsed), "flac lyrics")

    def test_no_tags_means_no_lyrics(self):
        self.assertEqual(lyrics_local.embedded_lyrics(Path("missing.flac"), mock.Mock(tags=None)), "")


if __name__ == "__main__":
    unittest.main()


class ReadTagsTests(unittest.TestCase):
    OPTS = {"only_severe": True}

    def _read(self, tags, name="Artist - Song.flac"):
        with mock.patch.object(library_stats.mutagen, "File", return_value=object()), \
             mock.patch.object(library_stats, "get_current_tags", return_value=dict(tags)), \
             mock.patch.object(library_stats, "process_audio_file", return_value={"lines": []}):
            return library_stats.read_tags(Path(name), self.OPTS)

    def test_a_song_with_title_and_artist_is_tagged(self):
        self.assertEqual(self._read({"title": "Song", "artist": "Artist"}), (False, []))

    def test_a_song_without_a_title_or_without_an_artist_counts_as_untagged(self):
        self.assertEqual(self._read({"title": None, "artist": "Artist"})[1], ["title"])
        self.assertEqual(self._read({"title": "Song", "artist": ""})[1], ["artist"])
        self.assertEqual(self._read({"title": None, "artist": None})[1], ["title", "artist"])

    def test_a_clearly_wrong_title_is_a_severe_mismatch(self):
        self.assertEqual(self._read({"title": "Totally Different Words", "artist": "A"}, "Moonlight Walk.flac"), (True, []))

    def test_a_file_mutagen_cannot_read_is_skipped(self):
        with mock.patch.object(library_stats.mutagen, "File", return_value=None):
            self.assertIsNone(library_stats.read_tags(Path("x.flac"), self.OPTS))


class ListUntaggedTests(unittest.TestCase):
    def test_the_tag_check_remembers_which_songs_lack_tags_and_the_list_shows_them(self):
        lib = library_stats.Library("/music")
        files = [Path("/music/b.flac"), Path("/music/a.flac"), Path("/music/ok.flac")]
        results = {files[0]: (False, ["artist"]), files[1]: (False, ["title", "artist"]), files[2]: (False, [])}
        with mock.patch.object(library_stats, "folder_problem", return_value=None), \
             mock.patch.object(lib, "files", return_value=files), \
             mock.patch.object(library_stats, "read_tags", side_effect=lambda p, o: results[p]):
            library_stats.tag_stats({"music_dir": "/music", "tags": {}}, 1, True, lib)
        self.assertEqual(lib.untagged, [(Path("/music/a.flac"), ["title", "artist"]), (Path("/music/b.flac"), ["artist"])])
        lines = []
        with mock.patch.object(library_stats, "log", side_effect=lines.append):
            library_stats.list_untagged(lib)
        text = "\n".join(lines)
        self.assertIn("a.flac", text)
        self.assertIn("no title or artist", text)
        self.assertIn("no artist", text)
