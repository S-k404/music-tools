import importlib.util
import io
import os
import shutil
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
        patch = mock.patch.object(dupes, "move_to_trash", side_effect=self.fake_trash)
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

    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["find_duplicates.py", str(self.lib), "--config", str(self.config),
                                             "--no-progress", *argv]), \
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

    def test_apply_needs_delete(self):
        with self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--apply")
        self.assertEqual(stop.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
