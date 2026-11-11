import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import identify_music as ident


class GenericDetectionTests(unittest.TestCase):
    def test_generic_stems(self):
        for stem in ("track01", "track 01", "track_1", "audiotrack_02", "audio01", "sound_12", "01", "1", "untitled"):
            info = ident.LocalTrackInfo(path=Path(f"{stem}.mp3"), duration=180.0, stem=stem, artist="Artist", title="Title")
            self.assertTrue(ident.is_generic_or_misnamed(info), stem)

    def test_missing_artist_or_title(self):
        info1 = ident.LocalTrackInfo(path=Path("song.mp3"), duration=180.0, stem="song", artist=None, title="Title")
        self.assertTrue(ident.is_generic_or_misnamed(info1))

        info2 = ident.LocalTrackInfo(path=Path("song.mp3"), duration=180.0, stem="song", artist="Unknown Artist", title="Title")
        self.assertTrue(ident.is_generic_or_misnamed(info2))

        info3 = ident.LocalTrackInfo(path=Path("song.mp3"), duration=180.0, stem="song", artist="Artist", title=None)
        self.assertTrue(ident.is_generic_or_misnamed(info3))

    def test_well_named_file_is_not_generic(self):
        info = ident.LocalTrackInfo(path=Path("Artist - Song.mp3"), duration=180.0, stem="Artist - Song",
                                   artist="Artist", title="Song")
        self.assertFalse(ident.is_generic_or_misnamed(info))


class EvaluationTests(unittest.TestCase):
    def test_no_match(self):
        info = ident.LocalTrackInfo(path=Path("01.mp3"), duration=180.0, stem="01")
        res = {"matches": []}
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "NO_MATCH")
        self.assertEqual(ev.confidence, 0.0)

    def test_reject_skew(self):
        info = ident.LocalTrackInfo(path=Path("01.mp3"), duration=180.0, stem="01")
        res = {
            "matches": [{"id": "1", "timeskew": 0.08, "frequencyskew": 0.0}],
            "track": {"title": "Title", "subtitle": "Artist"},
        }
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "REJECT_SKEW")

    @mock.patch.object(ident, "fetch_catalog_info")
    def test_exact_match_identified(self, mock_cat):
        mock_cat.return_value = ident.CatalogTrackInfo(
            title="Cheerleader", artist="Porter Robinson", album="SMILE! :D", duration=185.0
        )
        info = ident.LocalTrackInfo(path=Path("track01.mp3"), duration=185.0, stem="track01")
        res = {
            "matches": [{"id": "1", "timeskew": 0.0001, "frequencyskew": 0.0001}],
            "track": {"title": "Cheerleader", "subtitle": "Porter Robinson"},
        }
        ev = ident.evaluate_shazam_result(info, res, tolerance_seconds=3.0)
        self.assertEqual(ev.status, "IDENTIFIED")
        self.assertEqual(ev.confidence, 1.0)
        self.assertEqual(ev.proposed_artist, "Porter Robinson")
        self.assertEqual(ev.proposed_title, "Cheerleader")
        self.assertEqual(ev.proposed_filename, "Porter Robinson - Cheerleader.mp3")

    @mock.patch.object(ident, "fetch_catalog_info")
    def test_blocks_dj_mix(self, mock_cat):
        # File is 1 hour (3600s), but recognized single is 3 mins (180s)
        mock_cat.return_value = ident.CatalogTrackInfo(
            title="Baddadan", artist="Chase & Status", duration=185.0
        )
        info = ident.LocalTrackInfo(path=Path("boiler_room.mp3"), duration=3600.0, stem="boiler_room")
        res = {
            "matches": [{"id": "1", "timeskew": 0.0, "frequencyskew": 0.0}],
            "track": {"title": "Baddadan", "subtitle": "Chase & Status"},
        }
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "REJECT_MIX")
        self.assertEqual(ev.confidence, 0.0)
        self.assertIn("Likely DJ mix or live set", ev.detail)

    @mock.patch.object(ident, "fetch_catalog_info")
    def test_blocks_short_snippet(self, mock_cat):
        mock_cat.return_value = ident.CatalogTrackInfo(
            title="Song", artist="Artist", duration=240.0
        )
        info = ident.LocalTrackInfo(path=Path("clip.mp3"), duration=15.0, stem="clip")
        res = {
            "matches": [{"id": "1", "timeskew": 0.0, "frequencyskew": 0.0}],
            "track": {"title": "Song", "subtitle": "Artist"},
        }
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "REJECT_SNIPPET")

    @mock.patch.object(ident, "fetch_catalog_info")
    def test_warns_on_potential_remix(self, mock_cat):
        mock_cat.return_value = ident.CatalogTrackInfo(
            title="Glue", artist="Bicep", duration=269.0
        )
        info = ident.LocalTrackInfo(path=Path("Bicep - Glue (Kettama Remix).mp3"), duration=269.0,
                                   stem="Bicep - Glue (Kettama Remix)", artist="Bicep", title="Glue (Kettama Remix)")
        res = {
            "matches": [{"id": "1", "timeskew": 0.0, "frequencyskew": 0.0}],
            "track": {"title": "Glue", "subtitle": "Bicep"},
        }
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "POTENTIAL_REMIX")
        self.assertLessEqual(ev.confidence, 0.70)

    @mock.patch.object(ident, "fetch_catalog_info")
    def test_flags_mislabeled_file(self, mock_cat):
        mock_cat.return_value = ident.CatalogTrackInfo(
            title="Bohemian Rhapsody", artist="Queen", duration=354.0
        )
        info = ident.LocalTrackInfo(path=Path("Taylor Swift - Blank Space.mp3"), duration=354.0,
                                   stem="Taylor Swift - Blank Space", artist="Taylor Swift", title="Blank Space")
        res = {
            "matches": [{"id": "1", "timeskew": 0.0, "frequencyskew": 0.0}],
            "track": {"title": "Bohemian Rhapsody", "subtitle": "Queen"},
        }
        ev = ident.evaluate_shazam_result(info, res)
        self.assertEqual(ev.status, "MISLABELED")
        self.assertGreaterEqual(ev.confidence, 0.90)


class ApplyAndUndoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.music_dir = self.temp_dir / "Music"
        self.music_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_renames_audio_and_companion_sidecars(self):
        audio_file = self.music_dir / "track01.mp3"
        audio_file.write_bytes(b"dummy")
        lrc_file = self.music_dir / "track01.lrc"
        lrc_file.write_text("[00:01.00]test", encoding="utf-8")
        jpg_file = self.music_dir / "track01.jpg"
        jpg_file.write_bytes(b"dummyjpg")
        other_file = self.music_dir / "other.lrc"
        other_file.write_text("other", encoding="utf-8")

        local = ident.LocalTrackInfo(path=audio_file, duration=120.0, stem="track01")
        cat = ident.CatalogTrackInfo(title="New Title", artist="New Artist", album="New Album", duration=120.0)
        ev = ident.MatchEvaluation(
            local=local,
            catalog=cat,
            status="IDENTIFIED",
            confidence=1.0,
            duration_diff=0.0,
            detail="Identified",
            proposed_title="New Title",
            proposed_artist="New Artist",
            proposed_album="New Album",
            proposed_filename="New Artist - New Title.mp3",
        )

        # Mock MutagenFile write
        with mock.patch.object(ident, "MutagenFile", return_value=None):
            ok, undo_rec, acts = ident.apply_tags_and_rename(ev, update_tags=False, rename_file=True)

        self.assertTrue(ok)
        new_audio = self.music_dir / "New Artist - New Title.mp3"
        new_lrc = self.music_dir / "New Artist - New Title.lrc"
        new_jpg = self.music_dir / "New Artist - New Title.jpg"

        self.assertTrue(new_audio.is_file())
        self.assertTrue(new_lrc.is_file())
        self.assertTrue(new_jpg.is_file())
        self.assertTrue(other_file.is_file())
        self.assertFalse(audio_file.exists())
        self.assertFalse(lrc_file.exists())

        # Test reversing via undo log record
        for orig_sc, curr_sc in undo_rec.get("sidecar_moves", []):
            Path(curr_sc).rename(Path(orig_sc))
        Path(undo_rec["new_path"]).rename(Path(undo_rec["original_path"]))

        self.assertTrue(audio_file.is_file())
        self.assertTrue(lrc_file.is_file())
        self.assertTrue(jpg_file.is_file())
        self.assertFalse(new_audio.exists())


class FilenameIssueDetectionTests(unittest.TestCase):
    def test_leading_track_numbers(self):
        for stem in ("01 - People Need Love", "1. 2hollis - jeans", "01 A$AP Rocky - Trunks", "1-01 Track", "12_Title"):
            issue = ident.detect_filename_issue(stem)
            self.assertIsNotNone(issue, stem)

    def test_media_and_website_tags(self):
        self.assertEqual(ident.detect_filename_issue("Artist - Song [Official Music Video]"), "Video/media annotation in filename")
        self.assertEqual(ident.detect_filename_issue("Artist - Song (Visualizer)"), "Video/media annotation in filename")
        self.assertEqual(ident.detect_filename_issue("Artist - Song - SoundLoadMate.com"), "Website/ripper tag in filename")
        self.assertEqual(ident.detect_filename_issue("Artist - Song [SKIP TO 45 SECONDS]"), "Junk annotation in filename")

    def test_missing_artist_separator(self):
        issue = ident.detect_filename_issue("what u want!")
        self.assertEqual(issue, "Missing artist separator in filename")

    def test_clean_stems_are_not_flagged(self):
        self.assertIsNone(ident.detect_filename_issue("Porter Robinson - Cheerleader"))
        self.assertIsNone(ident.detect_filename_issue("Bicep - Glue (Kettama Remix)"))
        self.assertIsNone(ident.detect_filename_issue("ABBA - People Need Love"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.cache_file = self.temp_dir / "identify_cache.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_save_and_load_cache(self):
        with mock.patch.object(ident, "IDENTIFY_CACHE_FILE", self.cache_file):
            cache = {"tracks": {"test:100:1": {"shazam": {}, "catalog": None}}, "itunes": {}}
            ident.save_identify_cache(cache)
            loaded = ident.load_identify_cache()
            self.assertIn("test:100:1", loaded["tracks"])


if __name__ == "__main__":
    unittest.main()
