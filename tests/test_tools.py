import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import find_duplicates as dupes  # noqa: E402
from lyrics_lang import norm  # noqa: E402


def rec(duration, title="Title", artist="Artist", path="song.mp3", size=100, ext="mp3"):
    return dupes.Record(Path(path), artist, title, artist.lower(), title.lower(), duration, size, ext)


def key(title):
    """What build_records() actually groups on: tidy_title() need not be pretty, just collapse to
    the same norm() key as the plain title for re-downloads of the same edit."""
    return norm(dupes.tidy_title(title))


class TidyTitleTests(unittest.TestCase):
    def test_strips_youtube_noise(self):
        self.assertEqual(key("Song Title (Official Video)"), key("Song Title"))

    def test_strips_sped_up(self):
        self.assertEqual(key("Song Title (sped up)"), key("Song Title"))

    def test_strips_slowed_reverb_as_one_unit(self):
        self.assertEqual(key("Song Title (slowed + reverb)"), key("Song Title"))
        self.assertEqual(key("Song Title (slowed and reverb)"), key("Song Title"))

    def test_strips_nightcore(self):
        self.assertEqual(key("Song Title - Nightcore"), key("Song Title"))

    def test_strips_bracketed_remix(self):
        self.assertEqual(key("Song Title (Remix)"), key("Song Title"))

    def test_strips_trailing_live(self):
        self.assertEqual(key("Song Title - Live"), key("Song Title"))

    def test_real_title_with_live_is_not_mangled(self):
        # "live" is only stripped inside brackets or as a trailing " - live", never as a bare word,
        # so a real title that happens to contain it is left alone.
        self.assertEqual(key("Live and Let Die"), key("Live and Let Die"))
        self.assertNotEqual(key("Live and Let Die"), key("and Let Die"))


class ClusterByDurationTests(unittest.TestCase):
    def test_close_lengths_cluster_together(self):
        members = [rec(180.0), rec(181.0), rec(179.5)]
        clusters = dupes.cluster_by_duration(members, tolerance=3.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 3)

    def test_far_apart_lengths_split_into_separate_runs(self):
        # a song and a much-longer extended edit of the same title shouldn't be flagged together
        members = [rec(180.0), rec(181.0), rec(400.0)]
        clusters = dupes.cluster_by_duration(members, tolerance=3.0)
        self.assertEqual(len(clusters), 1)  # the lone 400s file isn't a pair, so it's dropped
        self.assertEqual([m.duration for m in clusters[0]], [180.0, 181.0])

    def test_singleton_clusters_are_dropped(self):
        members = [rec(100.0), rec(250.0), rec(400.0)]
        clusters = dupes.cluster_by_duration(members, tolerance=3.0)
        self.assertEqual(clusters, [])


class FindGroupsTests(unittest.TestCase):
    def test_groups_only_same_artist_and_title(self):
        records = [
            rec(180.0, title="A", artist="X", path="1.mp3"),
            rec(181.0, title="A", artist="X", path="2.mp3"),
            rec(180.0, title="A", artist="Y", path="3.mp3"),  # different artist, not a duplicate
            rec(180.0, title="B", artist="X", path="4.mp3"),  # different title, not a duplicate
        ]
        groups = dupes.find_groups(records, tolerance=3.0)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_larger_groups_sort_first(self):
        small = [rec(180.0, title="Small", artist="X", path="a.mp3", size=10),
                 rec(180.0, title="Small", artist="X", path="b.mp3", size=10)]
        big = [rec(180.0, title="Big", artist="X", path="c.mp3", size=1000),
               rec(180.0, title="Big", artist="X", path="d.mp3", size=1000)]
        groups = dupes.find_groups(small + big, tolerance=3.0)
        self.assertEqual(groups[0][0].title, "Big")


class FormattingTests(unittest.TestCase):
    def test_mmss(self):
        self.assertEqual(dupes.mmss(65), "1:05")
        self.assertEqual(dupes.mmss(5), "0:05")

    def test_human_size(self):
        self.assertEqual(dupes.human_size(500), "500 B")
        self.assertEqual(dupes.human_size(2048), "2.0 KB")
        self.assertEqual(dupes.human_size(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main()
