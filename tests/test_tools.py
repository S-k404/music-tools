import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

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


class StrayFixture(unittest.TestCase):
    """A small library on disk: files are real (so they can be deleted), their tags are made up."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def song(self, rel, **kw):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        return rec(180.0, path=str(path), ext=path.suffix.lstrip("."), **kw)


class InAlbumFolderTests(StrayFixture):
    def test_album_folder_artist_folder_and_root(self):
        self.assertIs(dupes.in_album_folder(self.root / "Ado" / "Show" / "a.mp3", self.root), True)
        self.assertIs(dupes.in_album_folder(self.root / "Ado" / "Show" / "Disc 1" / "a.mp3", self.root), True)
        self.assertIs(dupes.in_album_folder(self.root / "Ado" / "a.mp3", self.root), False)
        self.assertIs(dupes.in_album_folder(self.root / "a.mp3", self.root), False)

    def test_a_song_outside_the_music_folder_is_neither(self):
        self.assertIsNone(dupes.in_album_folder(Path(tempfile.gettempdir()) / "elsewhere" / "a" / "b.mp3", self.root))


class PlanStraysTests(StrayFixture):
    def test_keeps_the_album_copy_and_marks_the_loose_ones(self):
        keep = self.song("Ado/Show/Song.mp3")
        in_artist_folder = self.song("Ado/Song.mp3")
        in_root = self.song("Song.mp3")
        plans, no_album, nothing_loose = dupes.plan_strays([[keep, in_artist_folder, in_root]], self.root)
        self.assertEqual((no_album, nothing_loose), (0, 0))
        self.assertEqual([(p.keep, p.delete, p.held) for p in plans], [([keep], [in_artist_folder, in_root], [])])

    def test_two_album_copies_are_both_kept(self):
        a, b, loose = self.song("X/One/S.mp3"), self.song("X/Two/S.mp3"), self.song("S.mp3")
        plans, _, _ = dupes.plan_strays([[a, b, loose]], self.root)
        self.assertEqual((plans[0].keep, plans[0].delete), ([a, b], [loose]))

    def test_groups_without_an_album_copy_or_without_a_loose_copy_are_left_alone(self):
        both_loose = [self.song("S.mp3"), self.song("X/S.mp3")]
        both_albums = [self.song("Y/One/T.mp3"), self.song("Y/Two/T.mp3")]
        plans, no_album, nothing_loose = dupes.plan_strays([both_loose, both_albums], self.root)
        self.assertEqual((plans, no_album, nothing_loose), ([], 1, 1))

    def test_a_lossless_loose_copy_is_held_back_when_every_album_copy_is_lossy(self):
        lossy, lossless = self.song("A/Al/S.mp3"), self.song("S.flac")
        plans, _, _ = dupes.plan_strays([[lossy, lossless]], self.root)
        self.assertEqual((plans[0].delete, plans[0].held), ([], [lossless]))

    def test_a_lossless_loose_copy_goes_when_an_album_copy_is_lossless_too(self):
        keep, loose = self.song("A/Al/S.flac"), self.song("S.flac")
        plans, _, _ = dupes.plan_strays([[keep, loose]], self.root)
        self.assertEqual((plans[0].delete, plans[0].held), ([loose], []))

    def test_a_copy_outside_the_music_folder_is_never_touched(self):
        keep, loose = self.song("A/Al/S.mp3"), self.song("S.mp3")
        outside = rec(180.0, path=str(Path(tempfile.gettempdir()) / "elsewhere" / "S.mp3"))
        plans, _, _ = dupes.plan_strays([[keep, loose, outside]], self.root)
        self.assertEqual((plans[0].keep, plans[0].delete), ([keep], [loose]))


class DeleteStraysTests(StrayFixture):
    def run_delete(self, plans, trash=False):
        with mock.patch.object(dupes, "uses_trash", return_value=trash):
            return dupes.delete_strays(plans, self.root)

    def test_removes_the_loose_copy_with_its_lyrics_and_cover_but_not_a_lookalike(self):
        keep, loose = self.song("A/Al/S.mp3"), self.song("A/S.mp3")
        for name in ("S.lrc", "S.html", "S.jpg", "S.lrc.bak", "S.live.lrc", "Other.lrc"):
            (self.root / "A" / name).write_text("x")
        plan = dupes.StrayPlan([keep], [loose], [])
        removed, failed = self.run_delete([plan])
        self.assertEqual((removed, failed), ([(loose, keep)], 0))
        self.assertEqual(sorted(f.name for f in (self.root / "A").iterdir()), ["Al", "Other.lrc", "S.live.lrc"])
        self.assertTrue(Path(keep.path).is_file())

    def test_lyrics_stay_while_a_song_with_the_same_name_stays(self):
        # Song.flac is held back (lossless, the album copy isn't), so it keeps the Song.lrc it shares with Song.mp3
        keep, mp3, flac = self.song("A/Al/Song.mp3"), self.song("A/Song.mp3"), self.song("A/Song.flac")
        (self.root / "A" / "Song.lrc").write_text("x")
        removed, _ = self.run_delete([dupes.StrayPlan([keep], [mp3], [flac])])
        self.assertEqual(len(removed), 1)
        self.assertEqual(sorted(f.name for f in (self.root / "A").iterdir()), ["Al", "Song.flac", "Song.lrc"])

    def test_lyrics_go_with_the_last_of_two_loose_copies_that_share_a_name(self):
        keep, mp3, flac = self.song("A/Al/Song.flac"), self.song("A/Song.mp3"), self.song("A/Song.flac")
        (self.root / "A" / "Song.lrc").write_text("x")
        removed, _ = self.run_delete([dupes.StrayPlan([keep], [mp3, flac], [])])
        self.assertEqual(len(removed), 2)
        self.assertEqual(sorted(f.name for f in (self.root / "A").iterdir()), ["Al"])

    def test_folders_left_empty_are_removed_but_never_the_music_folder(self):
        keep, loose = self.song("A/Al/S.mp3"), self.song("Solo/Deep/S.mp3")
        removed, _ = self.run_delete([dupes.StrayPlan([keep], [loose], [])])
        self.assertEqual(len(removed), 1)
        self.assertFalse((self.root / "Solo").exists())
        self.assertTrue(self.root.is_dir())

    def test_a_failure_is_counted_and_leaves_the_other_files_alone(self):
        keep, a, b = self.song("A/Al/S.mp3"), self.song("S.mp3"), self.song("A/S.mp3")
        real = dupes.remove_file
        with mock.patch.object(dupes, "remove_file", side_effect=lambda p: "nope" if p == Path(a.path) else real(p)), \
                contextlib.redirect_stdout(io.StringIO()):
            removed, failed = self.run_delete([dupes.StrayPlan([keep], [a, b], [])])
        self.assertEqual((removed, failed), ([(b, keep)], 1))
        self.assertTrue(Path(a.path).is_file())

    def test_on_a_mac_the_trash_is_used_and_a_failed_trash_move_deletes_nothing(self):
        keep, loose = self.song("A/Al/S.mp3"), self.song("S.mp3")
        plan = dupes.StrayPlan([keep], [loose], [])
        with mock.patch.object(dupes, "move_to_trash", return_value=False) as trash, \
                contextlib.redirect_stdout(io.StringIO()):
            removed, failed = self.run_delete([plan], trash=True)
        self.assertEqual((removed, failed), ([], 1))
        trash.assert_called_once_with(Path(loose.path))
        self.assertTrue(Path(loose.path).is_file())


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed to make test audio")
class DeleteStraysCommandTests(unittest.TestCase):
    """The real command on a real (tiny) library: preview, refusal without a terminal, then --apply --yes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.lib = self.dir / "lib"
        for rel, artist, title, secs in (
                ("Ado/Show/Song One.mp3", "Ado", "Song One", 1), ("Ado/Song One.mp3", "Ado", "Song One", 1),
                ("Song One.mp3", "Ado", "Song One", 1), ("Ado/Show/Other.mp3", "Ado", "Other", 3),
                ("Solo Dup.mp3", "Solo", "Dup", 1), ("Misc/Solo Dup.mp3", "Solo", "Dup", 1)):
            path = self.lib / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                            "-t", str(secs), "-metadata", f"artist={artist}", "-metadata", f"title={title}", str(path)],
                           check=True)
        (self.lib / "Ado" / "Song One.lrc").write_text("[00:01.00]hi\n")
        self.config = self.dir / "c.toml"
        self.config.write_text(f"music_dir = {json.dumps(str(self.lib))}\n", encoding="utf-8")

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["find_duplicates.py", "--config", str(self.config), "--no-progress", *args]), \
                mock.patch.object(dupes, "start_log"), mock.patch.object(dupes, "LOG_DIR", self.dir / "logs"), \
                mock.patch.object(dupes, "uses_trash", return_value=False), \
                mock.patch.object(sys.stdin, "isatty", return_value=False), contextlib.redirect_stdout(out):
            try:
                code = dupes.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def songs_left(self):
        return sorted(str(p.relative_to(self.lib)).replace("\\", "/") for p in self.lib.rglob("*") if p.is_file())

    def test_preview_changes_nothing(self):
        before = self.songs_left()
        _, out = self.run_main("--delete-strays")
        self.assertIn("1 group", out)
        self.assertIn("2 loose copies", out)
        self.assertIn("Preview only", out)
        self.assertEqual(self.songs_left(), before)
        self.assertEqual(self.run_main("--delete-strays", "--dry-run")[0], None)
        self.assertEqual(self.songs_left(), before)

    def test_apply_needs_a_yes_without_a_terminal(self):
        before = self.songs_left()
        code, _ = self.run_main("--delete-strays", "--apply")
        self.assertEqual(code, "Nothing was changed.")
        self.assertEqual(self.songs_left(), before)

    def test_apply_removes_only_the_loose_copy_of_a_song_that_is_in_an_album(self):
        code, out = self.run_main("--delete-strays", "--apply", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 loose copies", out)
        self.assertEqual(self.songs_left(), ["Ado/Show/Other.mp3", "Ado/Show/Song One.mp3", "Misc/Solo Dup.mp3",
                                             "Solo Dup.mp3"])
        listed = next((self.dir / "logs").glob("duplicates_removed_*.txt")).read_text(encoding="utf-8")
        self.assertEqual(len(listed.splitlines()), 2)

    def test_the_plain_report_still_never_deletes(self):
        before = self.songs_left()
        _, out = self.run_main()
        self.assertIn("nothing was changed or deleted", out)
        self.assertEqual(self.songs_left(), before)


if __name__ == "__main__":
    unittest.main()
