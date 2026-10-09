import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import find_duplicates as dupes  # noqa: E402
import interactive  # noqa: E402
from mutagen.id3 import TIT2, TPE1  # noqa: E402
from mutagen.wave import WAVE  # noqa: E402
from lyrics_lang import norm  # noqa: E402


def rec(duration, title="Title", artist="Artist", path="song.mp3", size=100, ext="mp3", quality=None, has_album=False):
    return dupes.Record(Path(path), artist, title, artist.lower(), title.lower(), duration, size, ext,
                        quality or dupes.UNKNOWN_QUALITY, dupes.variant_markers(title), has_album)


def quality(ext, bitrate=0, rate=44100, bits=16, codec="", size=1000, duration=180.0):
    """audio_quality() for a file with these stream details (what mutagen would have read)."""
    info = SimpleNamespace(bitrate=bitrate, sample_rate=rate, bits_per_sample=bits, codec=codec)
    return dupes.audio_quality(info, ext, size, duration)


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
        self.assertIn("Nothing was changed", out)
        self.assertEqual(self.songs_left(), before)


class FitEndTests(unittest.TestCase):
    def test_a_long_path_keeps_its_file_name_and_every_row_is_the_same_width(self):
        long = "Artist Name/A Very Long Album Name Indeed/01 - Track Title.flac"
        cut = dupes.fit_end(long, 30)
        self.assertEqual(len(cut), 30)
        self.assertTrue(cut.startswith("…"))
        self.assertTrue(cut.rstrip().endswith("01 - Track Title.flac"))
        self.assertEqual(dupes.fit_end("a.mp3", 12), "a.mp3" + " " * 7)
        self.assertEqual(dupes.fit_end("歌" * 10, 11).count("歌"), 5)   # double-width characters count as two columns


class PlansFromPicksTests(unittest.TestCase):
    def test_what_is_not_picked_is_kept(self):
        a, b, c = rec(180.0, path="a.mp3"), rec(180.0, path="b.mp3"), rec(180.0, path="c.mp3")
        d = rec(180.0, title="Other", path="d.mp3")
        e = rec(180.0, title="Other", path="e.mp3")
        plans = dupes.plans_from_picks([[a, b, c], [d, e]], [(0, a), (0, c), (1, e)])
        self.assertEqual([(p.keep, p.delete) for p in plans], [([b], [a, c]), ([d], [e])])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed to make test audio")
class PickCopiesTests(unittest.TestCase):
    """`--pick`: every copy in one list, the rule's choices ticked, anything can be ticked except the last copy."""

    # rows come out in group order (the 3-copy song first), each group sorted by path
    ROWS = ["Ado/Show/Song One.mp3", "Ado/Song One.mp3", "Song One.mp3", "Misc/Solo Dup.mp3", "Solo Dup.mp3"]

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
        self.config = self.dir / "c.toml"
        self.config.write_text(f"music_dir = {json.dumps(str(self.lib))}\n", encoding="utf-8")
        self.shown, self.flashes = [], []

    def run_pick(self, *answers, confirm=True, args=("--pick",), tty=True):
        """Answer the tick list with these replies in turn (lists of booleans, or None to back out)."""
        replies = iter(answers)

        def fake_checklist(title, options, run_label, header=()):
            self.shown.append([list(o) for o in options])
            return next(replies)

        class Out(io.StringIO):   # the captured output stands in for the terminal
            def isatty(self):
                return tty

        out = Out()
        argv = ["find_duplicates.py", "--config", str(self.config), "--no-progress", *args]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(dupes, "start_log"), \
                mock.patch.object(dupes, "LOG_DIR", self.dir / "logs"), mock.patch.object(dupes, "uses_trash", return_value=False), \
                mock.patch.object(dupes, "confirm", return_value=confirm), \
                mock.patch.object(interactive, "checklist", side_effect=fake_checklist), \
                mock.patch.object(interactive, "flash", side_effect=lambda m: self.flashes.append(m)), \
                mock.patch.object(sys.stdin, "isatty", return_value=tty), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                code = dupes.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def left(self):
        return sorted(str(p.relative_to(self.lib)).replace("\\", "/") for p in self.lib.rglob("*.mp3"))

    def test_the_lists_starts_with_what_the_rule_would_remove_ticked(self):
        self.run_pick(None)
        rows = self.shown[0]
        self.assertEqual([r[0].strip() for r in rows], self.ROWS)
        self.assertEqual([r[1] for r in rows], [False, True, True, False, False])
        self.assertTrue(rows[0][2].startswith("in an album · 0:01 · "), rows[0][2])
        self.assertTrue(rows[1][2].startswith("loose · 0:01 · "), rows[1][2])

    def test_ticking_as_offered_removes_the_loose_copies(self):
        code, out = self.run_pick([False, True, True, False, False])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 copies", out)
        self.assertEqual(self.left(), ["Ado/Show/Other.mp3", "Ado/Show/Song One.mp3", "Misc/Solo Dup.mp3", "Solo Dup.mp3"])

    def test_a_group_the_rule_leaves_alone_can_still_be_picked_from(self):
        code, out = self.run_pick([False, False, False, True, False])   # both Solo Dup copies are loose
        self.assertEqual(code, 0)
        self.assertEqual(self.left(), ["Ado/Show/Other.mp3", "Ado/Show/Song One.mp3", "Ado/Song One.mp3",
                                       "Solo Dup.mp3", "Song One.mp3"])

    def test_the_last_copy_of_a_song_cannot_be_ticked(self):
        before = self.left()
        code, out = self.run_pick([True, True, True, False, False], [False, True, False, False, False])
        self.assertEqual(len(self.flashes), 1)
        self.assertIn("Ado — Song One", self.flashes[0])
        self.assertEqual([r[1] for r in self.shown[1]], [True, True, True, False, False])   # the ticks are remembered
        self.assertEqual(self.left(), [p for p in before if p != "Ado/Song One.mp3"])

    def test_backing_out_or_ticking_nothing_changes_nothing(self):
        before = self.left()
        self.run_pick(None)
        self.assertEqual(self.left(), before)
        _, out = self.run_pick([False] * 5)
        self.assertIn("Nothing was ticked", out)
        self.assertEqual(self.left(), before)

    def test_it_still_asks_before_removing(self):
        before = self.left()
        code, _ = self.run_pick([False, True, True, False, False], confirm=False)
        self.assertEqual(code, "Nothing was changed.")
        self.assertEqual(self.left(), before)

    def test_it_needs_a_terminal_and_does_not_combine_with_the_other_modes(self):
        code, _ = self.run_pick(tty=False)
        self.assertEqual(code, 2)
        for extra in ("--delete-strays", "--apply", "--yes", "--dry-run"):
            code, _ = self.run_pick(args=("--pick", extra))
            self.assertEqual(code, 2, extra)
        self.assertEqual(self.shown, [])


class QualityTests(unittest.TestCase):
    def better(self, a, b):
        return a.rank > b.rank

    def test_lossless_beats_even_a_320_mp3(self):
        self.assertTrue(self.better(quality("flac"), quality("mp3", 320000)))
        self.assertTrue(self.better(quality("wav"), quality("mp3", 320000)))

    def test_alac_in_an_m4a_counts_as_lossless(self):
        alac = quality("m4a", 900000, codec="alac")
        self.assertTrue(self.better(alac, quality("mp3", 320000)))
        self.assertTrue(alac.label.startswith("ALAC"))
        self.assertFalse(self.better(quality("m4a", 256000, codec="mp4a.40.2"), alac))

    def test_higher_resolution_lossless_wins(self):
        self.assertTrue(self.better(quality("flac", rate=96000, bits=24), quality("flac")))
        self.assertTrue(self.better(quality("flac", rate=48000), quality("flac", rate=44100)))

    def test_same_audio_as_flac_beats_wav_for_its_tags(self):
        self.assertTrue(self.better(quality("flac"), quality("wav")))

    def test_higher_bitrate_wins_within_a_format(self):
        self.assertTrue(self.better(quality("mp3", 320000), quality("mp3", 128000)))

    def test_efficient_codecs_count_for_more_per_kbps(self):
        self.assertTrue(self.better(quality("opus", 128000), quality("mp3", 128000)))
        self.assertTrue(self.better(quality("m4a", 256000), quality("mp3", 192000)))
        self.assertFalse(self.better(quality("opus", 96000), quality("mp3", 192000)))

    def test_a_near_tie_is_a_tie(self):
        self.assertEqual(quality("mp3", 128000).rank, quality("mp3", 131000).rank)   # VBR jitter, not a better file

    def test_a_file_that_states_no_bitrate_gets_one_from_its_size(self):
        q = quality("opus", bitrate=0, size=3_600_000, duration=240.0)   # 120 kbps
        self.assertEqual(q.label, "Opus ~120 kbps")

    def test_labels(self):
        self.assertEqual(quality("flac", rate=96000, bits=24).label, "FLAC 24-bit/96 kHz")
        self.assertEqual(quality("flac").label, "FLAC 16-bit/44.1 kHz")
        self.assertEqual(quality("mp3", 320000).label, "MP3 320 kbps")

    def test_unreadable_stream_info_still_ranks(self):
        q = dupes.audio_quality(None, "mp3", 4_000_000, 250.0)
        self.assertEqual(q.label, "MP3 ~128 kbps")


class VariantMarkerTests(unittest.TestCase):
    def test_plain_and_noisy_titles_have_none(self):
        self.assertEqual(dupes.variant_markers("Song"), frozenset())
        self.assertEqual(dupes.variant_markers("Song (Official Video)"), frozenset())
        self.assertEqual(dupes.variant_markers("Live and Let Die"), frozenset())

    def test_each_variant_is_spelled_one_way(self):
        self.assertEqual(dupes.variant_markers("Song (Instrumental)"), {"instrumental"})
        self.assertEqual(dupes.variant_markers("Song - Live"), {"live"})
        self.assertEqual(dupes.variant_markers("Song [Re-Mixed]"), {"remix"})
        self.assertEqual(dupes.variant_markers("Song (sped-up)"), dupes.variant_markers("Song Sped Up"))
        self.assertEqual(dupes.variant_markers("Song (slowed + reverb)"), {"slowed", "reverb"})
        self.assertEqual(dupes.variant_markers("Song (Alive)"), frozenset())


class PlanTests(unittest.TestCase):
    def test_the_best_quality_copy_is_kept(self):
        flac = rec(180.0, path="a.flac", ext="flac", quality=quality("flac"))
        mp3 = rec(180.0, path="b.mp3", quality=quality("mp3", 320000), size=10_000_000)
        low = rec(180.0, path="c.mp3", quality=quality("mp3", 128000))
        plan = dupes.make_plan([mp3, low, flac])
        self.assertIs(plan.keeper, flac)
        self.assertEqual([m.path.name for m in plan.losers], ["b.mp3", "c.mp3"])
        self.assertEqual(plan.review, "")

    def test_a_tie_goes_to_the_copy_with_an_album_then_the_bigger_file_then_the_first_path(self):
        q = quality("mp3", 192000)
        loose = rec(180.0, path="a.mp3", quality=q, size=900)
        filed = rec(180.0, path="b.mp3", quality=q, size=100, has_album=True)
        self.assertIs(dupes.choose_keeper([loose, filed]), filed)
        big, small = rec(180.0, path="a.mp3", quality=q, size=200), rec(180.0, path="b.mp3", quality=q, size=100)
        self.assertIs(dupes.choose_keeper([small, big]), big)
        one, two = rec(180.0, path="a.mp3", quality=q), rec(180.0, path="b.mp3", quality=q)
        self.assertIs(dupes.choose_keeper([two, one]), one)

    def test_different_versions_are_reported_but_never_removed(self):
        vocal = rec(180.0, title="Song", path="a.mp3", quality=quality("mp3", 320000))
        inst = rec(180.0, title="Song (Instrumental)", path="b.mp3", quality=quality("mp3", 128000))
        plan = dupes.make_plan([vocal, inst])
        self.assertEqual(plan.losers, [])
        self.assertIn("different versions", plan.review)
        self.assertEqual([dupes.mark(plan, m) for m in (vocal, inst)], ["", ""])

    def test_the_same_variant_in_both_copies_is_still_a_duplicate(self):
        a = rec(180.0, title="Song (Sped Up)", path="a.mp3", quality=quality("mp3", 320000))
        b = rec(180.0, title="Song - Sped Up", path="b.mp3", quality=quality("mp3", 128000))
        self.assertEqual(len(dupes.make_plan([a, b]).losers), 1)

    def test_two_names_for_one_file_are_not_removed(self):
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / "a.mp3"
            first.write_bytes(b"x")
            os.link(first, Path(d) / "b.mp3")
            a = rec(180.0, path=str(first), quality=quality("mp3", 320000))
            b = rec(180.0, path=str(Path(d) / "b.mp3"), quality=quality("mp3", 128000))
            self.assertEqual(dupes.make_plan([a, b]).losers, [])


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.trashed = []
        for patch in (mock.patch.object(dupes, "uses_trash", return_value=True),   # the Mac path; elsewhere files are deleted
                      mock.patch.object(dupes, "move_to_trash", side_effect=self.fake_trash)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def fake_trash(self, path):
        self.trashed.append(Path(path).name)
        Path(path).unlink()
        return True

    def song(self, rel, text="x"):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def record(self, path):
        return rec(180.0, path=str(path), quality=quality("mp3", 128000))

    def test_the_lower_copy_goes_to_the_trash_with_its_own_files(self):
        keep, lose = self.song("a/Song.flac"), self.song("b/Song.mp3")
        self.song("b/Song.lrc"), self.song("b/Song.jpg")
        self.song("a/Song.lrc", "keepers")                 # the copy kept already has lyrics
        ok, note = dupes.remove_copy(self.record(keep), self.record(lose))
        self.assertTrue(ok)
        self.assertEqual(sorted(self.trashed), ["Song.jpg", "Song.lrc", "Song.mp3"])
        self.assertEqual((self.dir / "a/Song.lrc").read_text(), "keepers")
        self.assertTrue(keep.exists())

    def test_lyrics_the_kept_copy_lacks_move_over_to_it(self):
        keep, lose = self.song("a/Other Name.flac"), self.song("b/Song.mp3")
        self.song("b/Song.lrc", "words"), self.song("b/Song.html", "page"), self.song("b/Song.lrc.bak", "orig")
        ok, note = dupes.remove_copy(self.record(keep), self.record(lose))
        self.assertTrue(ok)
        self.assertEqual(self.trashed, ["Song.mp3"])
        self.assertEqual((self.dir / "a/Other Name.lrc").read_text(), "words")
        self.assertEqual((self.dir / "a/Other Name.html").read_text(), "page")
        self.assertEqual((self.dir / "a/Other Name.lrc.bak").read_text(), "orig")
        self.assertFalse((self.dir / "b/Song.lrc").exists())
        self.assertIn(".lrc moved to the copy kept", note)

    def test_files_shared_with_another_copy_of_the_same_name_are_left_alone(self):
        keep, lose = self.song("a/Song.flac"), self.song("a/Song.mp3")   # one Song.lrc serves both
        self.song("a/Song.lrc", "shared")
        self.assertEqual(dupes.own_sidecars(lose), [])
        dupes.remove_copy(self.record(keep), self.record(lose))
        self.assertEqual(self.trashed, ["Song.mp3"])
        self.assertEqual((self.dir / "a/Song.lrc").read_text(), "shared")

    def test_only_files_named_exactly_like_the_song_count(self):
        lose = self.song("b/Song.mp3")
        self.song("b/Song.Remix.lrc"), self.song("b/Song Part 2.lrc"), self.song("b/Song.lrc")
        self.assertEqual([f.name for f in dupes.own_sidecars(lose)], ["Song.lrc"])

    def test_shared_folder_pictures_are_never_taken(self):
        lose = self.song("b/cover.mp3")
        self.song("b/cover.jpg")                            # a song that happens to be called "cover"
        self.assertEqual(dupes.own_sidecars(lose), [])

    def test_nothing_is_removed_when_the_copy_to_keep_is_gone(self):
        lose = self.song("b/Song.mp3")
        ok, note = dupes.remove_copy(self.record(self.dir / "a/Song.flac"), self.record(lose))
        self.assertFalse(ok)
        self.assertTrue(lose.exists())
        self.assertEqual(self.trashed, [])

    def test_a_refused_trash_leaves_the_copy_and_its_files_in_place(self):
        keep, lose = self.song("a/Song.flac"), self.song("b/Song.mp3")
        self.song("b/Song.lrc")
        with mock.patch.object(dupes, "move_to_trash", return_value=False):
            ok, note = dupes.remove_copy(self.record(keep), self.record(lose))
        self.assertFalse(ok)
        self.assertTrue(lose.exists() and (self.dir / "b/Song.lrc").exists())

    def test_without_a_trash_the_lower_copy_and_its_files_are_deleted_for_good(self):
        keep, lose = self.song("a/Song.flac"), self.song("b/Song.mp3")
        self.song("b/Song.lrc"), self.song("a/Song.lrc", "keepers")
        with mock.patch.object(dupes, "uses_trash", return_value=False):
            ok, note = dupes.remove_copy(self.record(keep), self.record(lose))
        self.assertTrue(ok)
        self.assertEqual(self.trashed, [])                  # the Trash was never asked
        self.assertFalse((self.dir / "b/Song.mp3").exists() or (self.dir / "b/Song.lrc").exists())
        self.assertEqual((self.dir / "a/Song.lrc").read_text(), "keepers")

    def test_remove_losers_counts_cleans_empty_folders_and_lists_what_was_kept(self):
        keep, lose = self.song("a/Song.flac"), self.song("b/Song.mp3")
        plan = dupes.Plan([], self.record(keep), [self.record(lose)])
        with redirect_stdout(io.StringIO()):
            kept, removed, failed, freed = dupes.remove_losers([plan], str(self.dir), True)
        self.assertEqual((kept, removed, failed), ([keep], 1, 0))
        self.assertFalse((self.dir / "b").exists())         # nothing left in it
        self.assertTrue((self.dir / "a").is_dir())

    def test_it_gives_up_after_three_failures_in_a_row(self):
        keep = self.song("a/Song.flac")
        plans = [dupes.Plan([], self.record(keep), [self.record(self.song(f"b/Song {i}.mp3"))]) for i in range(5)]
        with mock.patch.object(dupes, "move_to_trash", return_value=False), redirect_stdout(io.StringIO()):
            kept, removed, failed, freed = dupes.remove_losers(plans, str(self.dir), True)
        self.assertEqual((kept, removed, failed), ([], 0, 3))


class FixAlbumsTests(unittest.TestCase):
    def test_songs_kept_inside_the_library_are_handed_to_the_organizer(self):
        with tempfile.TemporaryDirectory() as d:
            inside, outside = Path(d) / "lib" / "a.flac", Path(d) / "elsewhere" / "b.flac"
            for f in (inside, outside):
                f.parent.mkdir(parents=True)
                f.write_text("x")
            with mock.patch("interactive.run_tool", return_value=0) as run, redirect_stdout(io.StringIO()):
                code = dupes.fix_albums([inside, outside], str(Path(d) / "lib"), "my.toml", True)
        run.assert_called_once_with("organize", ["--config", "my.toml", "--no-progress", str(inside)])
        self.assertEqual(code, 0)

    def test_the_album_lookup_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as d:
            song = Path(d) / "a.flac"
            song.write_text("x")
            with mock.patch("interactive.run_tool", return_value=0) as run, redirect_stdout(io.StringIO()):
                dupes.fix_albums([song], d, None, False, lookup=False)
        run.assert_called_once_with("organize", ["--no-auto-album", str(song)])

    def test_nothing_runs_when_every_kept_song_is_outside_the_library(self):
        with tempfile.TemporaryDirectory() as d:
            song = Path(d) / "elsewhere" / "b.flac"
            song.parent.mkdir()
            song.write_text("x")
            with mock.patch("interactive.run_tool") as run, redirect_stdout(io.StringIO()):
                self.assertEqual(dupes.fix_albums([song], str(Path(d) / "lib"), None, False), 0)
        run.assert_not_called()

    def test_the_organizer_failing_shows_in_the_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            song = Path(d) / "a.flac"
            song.write_text("x")
            with mock.patch("interactive.run_tool", return_value=1), redirect_stdout(io.StringIO()):
                self.assertEqual(dupes.fix_albums([song], d, None, False), 1)

    def test_a_long_list_is_split_into_runs(self):
        batches = list(dupes.in_batches(["x" * 10] * 10, limit=35))
        self.assertEqual([len(b) for b in batches], [3, 3, 3, 1])
        self.assertEqual(list(dupes.in_batches([])), [])


def write_wav(path, bits, artist="Artist"):
    """A real, readable one-second silent WAV, tagged with an artist and the title from its name (the stdlib can
    write the audio, so no ffmpeg is needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(bits // 8)
        w.setframerate(8000)
        w.writeframes(b"\x00" * (bits // 8) * 8000)
    song = WAVE(path)
    song.add_tags()
    song.tags.add(TPE1(encoding=3, text=[artist]))
    song.tags.add(TIT2(encoding=3, text=[path.stem.split(" - ")[-1]]))
    song.save()


class EndToEndTests(unittest.TestCase):
    """The real tag reader and grouping on real files; only the Trash and the organizer run are stood in."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lib = self.dir / "lib"
        write_wav(self.lib / "YouTube/Artist - Song.wav", 16)
        write_wav(self.lib / "Artist/Album/Artist - Song.wav", 24)
        write_wav(self.lib / "YouTube/Artist - Different.wav", 16)
        self.config = self.dir / "config.toml"
        self.config.write_text(f'music_dir = "{self.lib}"\nsave_logs = false\n')
        self.trash = self.dir / "trash"
        self.trash.mkdir()

    def fake_trash(self, path):
        shutil.move(str(path), str(self.trash / path.name))
        return True

    def run_main(self, *argv, trash=True):
        """trash=False is Windows or Linux: nothing to move files to, so they are deleted."""
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["find_duplicates.py", str(self.lib), "--config", str(self.config),
                                             "--no-progress", *argv]), \
                mock.patch.object(dupes, "uses_trash", return_value=trash), \
                mock.patch.object(dupes, "move_to_trash", side_effect=self.fake_trash), \
                mock.patch("interactive.run_tool", return_value=0) as organizer, redirect_stdout(out):
            code = dupes.main()
        return code, out.getvalue(), organizer

    def files(self):
        return sorted(str(p.relative_to(self.lib)) for p in self.lib.rglob("*.wav"))

    def test_the_report_changes_nothing_and_says_which_copy_wins(self):
        before = self.files()
        code, out, organizer = self.run_main()
        self.assertEqual(self.files(), before)
        self.assertIn("keep", out)
        self.assertIn("WAV 24-bit/8 kHz", out)
        self.assertIn("Nothing was changed", out)
        organizer.assert_not_called()

    def test_delete_alone_only_previews(self):
        before = self.files()
        code, out, organizer = self.run_main("--delete")
        self.assertEqual(self.files(), before)
        self.assertIn("Preview only", out)
        organizer.assert_not_called()

    def test_apply_keeps_the_higher_resolution_copy_then_fixes_its_album(self):
        code, out, organizer = self.run_main("--delete", "--apply", "--yes")
        self.assertEqual(self.files(), ["Artist/Album/Artist - Song.wav", "YouTube/Artist - Different.wav"])
        self.assertEqual(os.listdir(self.trash), ["Artist - Song.wav"])
        self.assertIn("1 copy moved to the Trash", out)
        kept = str(self.lib / "Artist/Album/Artist - Song.wav")
        organizer.assert_called_once_with("organize", ["--config", str(self.config), "--no-progress", kept])
        self.assertEqual(code, 0)

    def test_without_a_trash_the_lower_copy_is_deleted_and_the_message_says_so(self):
        code, out, organizer = self.run_main("--delete", "--apply", "--yes", trash=False)
        self.assertEqual(self.files(), ["Artist/Album/Artist - Song.wav", "YouTube/Artist - Different.wav"])
        self.assertEqual(os.listdir(self.trash), [])
        self.assertIn("1 copy deleted", out)
        self.assertNotIn("Trash", out)
        self.assertEqual(code, 0)

    def test_the_question_says_where_the_copies_go(self):
        for trash, words in ((True, "to the Trash"), (False, "can't be brought back")):
            asked = []
            with mock.patch.object(dupes, "confirm", side_effect=lambda q: asked.append(q) or False), \
                    self.assertRaises(SystemExit):
                self.run_main("--delete", "--apply", trash=trash)
            self.assertIn(words, asked[0])

    def test_no_fix_albums_skips_the_organizer(self):
        code, out, organizer = self.run_main("--delete", "--apply", "--yes", "--no-fix-albums")
        self.assertEqual(len(self.files()), 2)
        organizer.assert_not_called()

    def test_without_yes_a_script_run_changes_nothing(self):
        before = self.files()
        with mock.patch.object(sys, "stdin", io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.run_main("--delete", "--apply")
        self.assertIn("Nothing was changed", str(stop.exception))
        self.assertEqual(self.files(), before)

    def test_apply_needs_a_rule_to_apply(self):
        with self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--apply")
        self.assertEqual(stop.exception.code, 2)

    def test_the_two_rules_do_not_combine(self):
        with self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.run_main("--delete", "--delete-strays")
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("pick one rule", err.getvalue())

    def test_the_picker_does_not_combine_with_either_rule(self):
        for rule in ("--delete", "--delete-strays"):
            with self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                self.run_main("--pick", rule)
            self.assertEqual(stop.exception.code, 2, rule)

    def test_the_loose_copy_rule_still_works_beside_the_quality_rule(self):
        before = self.files()
        code, out, organizer = self.run_main("--delete-strays")
        self.assertEqual(self.files(), before)             # a preview
        self.assertIn("Preview only", out)
        organizer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
