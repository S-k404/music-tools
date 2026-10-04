import importlib.util
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import find_artist_art as artists  # noqa: E402


class CombinationNameTests(unittest.TestCase):
    def test_the_ways_collaborations_are_written(self):
        parts = artists.name_parts
        self.assertEqual(parts("Skrillex with The Ragga Twins"), ["Skrillex", "The Ragga Twins"])
        self.assertEqual(parts("Nina Kraviz as Bara Nova"), ["Nina Kraviz", "Bara Nova"])
        self.assertEqual(parts("Lotus Juice / Azumi Takahashi / ATLUS Sound Team"),
                         ["Lotus Juice", "Azumi Takahashi", "ATLUS Sound Team"])
        self.assertEqual(parts("MEMPHX/CLXUD/akrai"), ["MEMPHX", "CLXUD", "akrai"])
        self.assertEqual(parts("宵崎奏&朝比奈まふゆ&東雲絵名"), ["宵崎奏", "朝比奈まふゆ", "東雲絵名"])
        self.assertEqual(parts("井芹仁菜、河原木桃香"), ["井芹仁菜", "河原木桃香"])
        self.assertEqual(parts("Shonci × bbno$ × Puterrier"), ["Shonci", "bbno$", "Puterrier"])
        self.assertEqual(parts("サカモト教授 feat.初音ミク"), ["サカモト教授", "初音ミク"])
        self.assertEqual(parts("稲葉曇 vo. 歌愛ユキ"), ["稲葉曇", "歌愛ユキ"])

    def test_single_artists_are_not_split(self):
        for name in ("Radiohead", "Cidro Onetoo", "Alexei Brayko", "Dukes of Azure", "Baby Jane", "Wasabi"):
            self.assertEqual(artists.name_parts(name), [], name)

    def test_search_variants_drop_brackets_and_youtube_topic_suffix(self):
        self.assertEqual(artists.query_variants("Mr. Smiirk - Topic"), ["Mr. Smiirk - Topic", "Mr. Smiirk"])
        self.assertEqual(artists.query_variants("千石撫子(CV:花澤香菜)"), ["千石撫子(CV:花澤香菜)", "千石撫子", "花澤香菜"])
        self.assertEqual(artists.query_variants("桜島麻衣（CV:瀬戸麻沙美）"),
                         ["桜島麻衣（CV:瀬戸麻沙美）", "桜島麻衣", "瀬戸麻沙美"])
        self.assertEqual(artists.query_variants("ミア・テイラー (CV.内田 秀)")[-1], "内田 秀")
        self.assertEqual(artists.query_variants("マヨネーズ.exe【Nekotaro】"),
                         ["マヨネーズ.exe【Nekotaro】", "マヨネーズ.exe", "Nekotaro"])
        self.assertEqual(artists.query_variants("Radiohead"), ["Radiohead"])

    def test_a_voice_actor_or_alias_counts_as_the_artist(self):
        found = [{"name": "花澤香菜", "nb_fan": 5, "picture_xl": "https://x/p.jpg"}]
        self.assertEqual(artists.exact_match(found, "千石撫子(CV:花澤香菜)")[0], found[0])
        self.assertIsNone(artists.exact_match(found, "千石撫子")[0])


class GenreWordTests(unittest.TestCase):
    def test_a_genre_in_the_artist_tag_is_not_looked_up_as_an_artist(self):
        looked_up = []
        found = {"ZARA": {"name": "ZARA", "link": "z", "picture_xl": "https://x/z.jpg", "nb_fan": 5}}

        def fake_find(name):
            looked_up.append(name)
            return found.get(name), "", []
        job = artists.Job("KPOP, House, ZARA")
        with mock.patch.object(artists, "_find", side_effect=fake_find):
            artists._search_job(job, {"search_results": 3})
        self.assertEqual(looked_up, ["KPOP, House, ZARA", "ZARA"])
        self.assertEqual([p.name for p in job.picks], ["ZARA"])


class DroppedConnectionTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(artists.time, "sleep"), mock.patch.object(artists.API_LIMIT, "wait")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_dropped_ssl_connection_is_retried(self):
        drop = urllib.error.URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
        with mock.patch.object(artists, "fetch_url", side_effect=[drop, drop, b'{"data": []}']) as fetch:
            self.assertEqual(artists.deezer_get("search/artist", q="x"), {"data": []})
        self.assertEqual(fetch.call_count, 3)

    def test_being_offline_is_not_retried(self):
        gone = urllib.error.URLError(OSError("nodename nor servname provided"))
        with mock.patch.object(artists, "fetch_url", side_effect=gone) as fetch:
            with self.assertRaises(RuntimeError):
                artists.deezer_get("search/artist", q="x")
        self.assertEqual(fetch.call_count, 1)

    def test_it_gives_up_after_a_few_drops(self):
        drop = urllib.error.URLError(ssl.SSLEOFError(8, "EOF"))
        with mock.patch.object(artists, "fetch_url", side_effect=drop) as fetch:
            with self.assertRaises(RuntimeError):
                artists.deezer_get("search/artist", q="x")
        self.assertEqual(fetch.call_count, 4)

    def test_an_ssl_eof_on_a_pooled_connection_counts_as_stale(self):
        self.assertIn(ssl.SSLEOFError, common._STALE)


    def _stub_youtube(self, videos, channels):
        def entries(url):
            return videos if "CAMSAhAB" in url else channels
        real = artists._youtube_entries
        artists._youtube_entries = entries
        self.addCleanup(setattr, artists, "_youtube_entries", real)

    def test_popular_video_channels_rank_by_views_and_keep_only_channels_with_a_picture(self):
        video = lambda cid, ch, views: {"ie_key": "Youtube", "channel_id": cid, "channel": ch, "view_count": views}
        chan = lambda cid, ch: {"ie_key": "YoutubeTab", "channel": ch, "channel_url": "https://youtube.com/channel/" + cid,
                                "channel_follower_count": 7, "thumbnails": [{"url": "https://x/a=s88-c", "width": 88}]}
        self._stub_youtube([video("A", "Small", 10), video("B", "Big", 900), video("B", "Big", 100), video("C", "NoPic", 5000)],
                           [chan("A", "Small"), chan("B", "Big")])
        got = artists.popular_video_channels("whoever")
        self.assertEqual([c["name"] for c in got], ["Big", "Small"])
        self.assertTrue(got[0]["picture_xl"].startswith("https://x/a=s800-"))


if __name__ == "__main__":
    unittest.main()
