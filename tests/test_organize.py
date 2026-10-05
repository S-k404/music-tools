import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import organize_music as org
import run_all


class SanitizeTests(unittest.TestCase):
    def test_replaces_illegal_characters(self):
        self.assertEqual(org.sanitize_name("AC/DC"), "AC-DC")
        self.assertEqual(org.sanitize_name("Artist: Name"), "Artist - Name")
        self.assertEqual(org.sanitize_name('Song "Remix"? *Live*'), "Song Remix Live")

    def test_cleans_trailing_dots_and_spaces(self):
        self.assertEqual(org.sanitize_name("Fred again.. "), "Fred again")
        self.assertEqual(org.sanitize_name("   Album Name.  "), "Album Name")

    def test_fallback_on_empty(self):
        self.assertEqual(org.sanitize_name("", fallback="Unknown Artist"), "Unknown Artist")
        self.assertEqual(org.sanitize_name("///", fallback="Singles"), "Singles")


class SidecarAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.music_dir = Path(self.temp_dir) / "Music"
        self.music_dir.mkdir()
        self.source_dir = self.music_dir / "YouTube"
        self.source_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_find_sidecars(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"")
        lrc = self.source_dir / "Artist - Title.lrc"
        lrc.write_text("[00:01.00]lyrics", encoding="utf-8")
        html = self.source_dir / "Artist - Title.html"
        html.write_text("<html></html>", encoding="utf-8")
        bak = self.source_dir / "Artist - Title.lrc.bak"
        bak.write_text("backup", encoding="utf-8")
        unrelated = self.source_dir / "Other - Song.lrc"
        unrelated.write_text("other", encoding="utf-8")

        sidecars = org.find_sidecars(song)
        self.assertEqual(len(sidecars), 3)
        self.assertIn(lrc, sidecars)
        self.assertIn(html, sidecars)
        self.assertIn(bak, sidecars)
        self.assertNotIn(unrelated, sidecars)

    def test_plan_move_with_fallback_singles(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"")
        plan = org.plan_move(song, self.music_dir, fallback_artist="Unknown", fallback_album="Singles")
        self.assertEqual(plan.artist, "Artist")
        self.assertEqual(plan.album, "Singles")
        expected_dest = self.music_dir / "Artist" / "Singles" / "Artist - Title.mp3"
        self.assertEqual(plan.dest_audio, expected_dest)
        self.assertFalse(plan.is_noop)

    def test_tags_that_say_null_or_unknown_count_as_missing(self):
        import unittest.mock as mock
        song = self.source_dir / "Real Artist - Real Title.mp3"
        song.write_bytes(b"")
        for album in ("null", "Unknown", "Unknown Album", "N/A"):
            tags = {"artist": ["null"], "album": [album], "title": ["Real Title"]}
            with mock.patch.object(org, "MutagenFile", return_value=mock.Mock(tags=tags)):
                meta = org.read_track_meta(song)
                plan = org.plan_move(song, self.music_dir, fallback_album="Singles")
            self.assertEqual((meta["artist"], meta["album"]), ("Real Artist", ""), album)
            self.assertEqual((plan.artist, plan.album), ("Real Artist", "Singles"), album)

    def test_a_placeholder_album_from_the_lookup_cache_is_not_used(self):
        import json
        import unittest.mock as mock
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"")
        meta = {"artist": "Artist", "album": "Title", "title": "Title", "track": None}
        plan = org.plan_move(song, self.music_dir, fallback_album="Singles", resolved_album="Unknown", track_meta=meta)
        self.assertEqual(plan.album, "Singles")
        cache = Path(self.temp_dir) / "cache.json"
        cache.write_text(json.dumps({"a // one": "Unknown", "a // two": "null", "a // three": "Real Album", "a // four": ""}), encoding="utf-8")
        with mock.patch.object(org, "ALBUM_CACHE_FILE", cache):
            self.assertEqual(org.load_album_cache(), {"a // three": "Real Album", "a // four": ""})

    def test_plan_move_flat_when_fallback_album_empty(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"")
        plan = org.plan_move(song, self.music_dir, fallback_artist="Unknown", fallback_album="")
        self.assertEqual(plan.artist, "Artist")
        self.assertEqual(plan.album, "")
        expected_dest = self.music_dir / "Artist" / "Artist - Title.mp3"
        self.assertEqual(plan.dest_audio, expected_dest)

    def test_plan_move_collision_resolution(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"source content")
        dest_dir = self.music_dir / "Artist" / "Singles"
        dest_dir.mkdir(parents=True)
        existing = dest_dir / "Artist - Title.mp3"
        existing.write_bytes(b"existing content")

        plan = org.plan_move(song, self.music_dir, fallback_album="Singles")
        self.assertEqual(plan.dest_audio.name, "Artist - Title (2).mp3")

    def test_execute_plan_moves_song_and_sidecars(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"audio")
        lrc = self.source_dir / "Artist - Title.lrc"
        lrc.write_text("lyrics", encoding="utf-8")

        plan = org.plan_move(song, self.music_dir, fallback_album="Singles")
        success = org.execute_plan(plan, dry_run=False)
        self.assertTrue(success)
        self.assertFalse(song.exists())
        self.assertFalse(lrc.exists())
        self.assertTrue((self.music_dir / "Artist" / "Singles" / "Artist - Title.mp3").exists())
        self.assertTrue((self.music_dir / "Artist" / "Singles" / "Artist - Title.lrc").exists())

    def test_execute_plan_dry_run_touches_nothing(self):
        song = self.source_dir / "Artist - Title.mp3"
        song.write_bytes(b"audio")
        plan = org.plan_move(song, self.music_dir, fallback_album="Singles")
        success = org.execute_plan(plan, dry_run=True)
        self.assertTrue(success)
        self.assertTrue(song.exists())
        self.assertFalse((self.music_dir / "Artist" / "Singles" / "Artist - Title.mp3").exists())

    def test_copy_jellyfin_artist_art(self):
        artist_dir = self.music_dir / "Artist"
        artist_dir.mkdir()
        artist_art_dir = self.music_dir / "Artist Art"
        artist_art_dir.mkdir()
        photo = artist_art_dir / "Artist.jpg"
        photo.write_bytes(b"jpeg_data")

        copied = org.copy_jellyfin_artist_art(self.music_dir, "Artist", artist_art_dir, dry_run=False)
        self.assertTrue(copied)
        jellyfin_folder_jpg = artist_dir / "folder.jpg"
        self.assertTrue(jellyfin_folder_jpg.is_file())
        self.assertEqual(jellyfin_folder_jpg.read_bytes(), b"jpeg_data")

        # Second time should be no-op because it already exists
        self.assertFalse(org.copy_jellyfin_artist_art(self.music_dir, "Artist", artist_art_dir, dry_run=False))

    def test_places_inside_existing_folder(self):
        # Create an existing artist & album folder with an existing song
        existing_dir = self.music_dir / "Daft Punk" / "Discovery"
        existing_dir.mkdir(parents=True)
        (existing_dir / "01 - One More Time.mp3").write_bytes(b"existing track")

        # New song to organize into the same artist & album
        new_song = self.source_dir / "Daft Punk - Aerodynamic.mp3"
        new_song.write_bytes(b"new track")

        plan = org.plan_move(new_song, self.music_dir, fallback_album="Discovery")
        self.assertEqual(plan.dest_audio, existing_dir / "Daft Punk - Aerodynamic.mp3")

        # Execute and ensure both files now live together in the existing folder
        ok = org.execute_plan(plan, dry_run=False)
        self.assertTrue(ok)
        self.assertTrue((existing_dir / "01 - One More Time.mp3").exists())
        self.assertTrue((existing_dir / "Daft Punk - Aerodynamic.mp3").exists())

    def test_copy_jellyfin_artist_art_dry_run_for_new_folder(self):
        artist_art_dir = self.music_dir / "Artist Art"
        artist_art_dir.mkdir()
        (artist_art_dir / "NewArtist.jpg").write_bytes(b"data")

        # Folder does not exist yet on disk
        self.assertFalse((self.music_dir / "NewArtist").exists())
        # In dry run, should return True because it would be copied once folder is created
        copied = org.copy_jellyfin_artist_art(self.music_dir, "NewArtist", artist_art_dir, dry_run=True)
        self.assertTrue(copied)
        self.assertFalse((self.music_dir / "NewArtist").exists())

    def test_plan_move_case_insensitive_reuse(self):
        # Existing folder with specific casing
        existing_dir = self.music_dir / "Kinji"
        existing_dir.mkdir()

        song = self.source_dir / "kinji - Track.mp3"
        song.write_bytes(b"data")

        plan = org.plan_move(song, self.music_dir)
        self.assertEqual(plan.artist, "Kinji")
        self.assertEqual(plan.dest_audio.parent, existing_dir / "Singles")

    def test_plan_move_with_auto_detected_album(self):
        song = self.source_dir / "Daft Punk - One More Time.mp3"
        song.write_bytes(b"data")

        # When resolved_album is provided, it goes into Discovery instead of Singles
        plan = org.plan_move(song, self.music_dir, resolved_album="Discovery")
        self.assertEqual(plan.artist, "Daft Punk")
        self.assertEqual(plan.album, "Discovery")
        self.assertTrue(plan.auto_album)
        expected = self.music_dir / "Daft Punk" / "Discovery" / "Daft Punk - One More Time.mp3"
        self.assertEqual(plan.dest_audio, expected)

    def test_clean_query_title(self):
        self.assertEqual(org.clean_query_title("Song Title (feat. Artist)"), "Song Title")
        self.assertEqual(org.clean_query_title("Track [Official Video]"), "Track")
        self.assertEqual(org.clean_query_title("Simple"), "Simple")

    def test_clean_empty_directories(self):
        (self.source_dir / "keep.mp3").write_bytes(b"audio")
        empty_sub = self.source_dir / "Mixes" / "Old"
        empty_sub.mkdir(parents=True)
        (empty_sub / ".DS_Store").write_bytes(b"junk")

        removed = org.clean_empty_directories([empty_sub], self.music_dir)
        self.assertEqual(removed, 2)  # Old and Mixes
        self.assertFalse(empty_sub.exists())
        self.assertFalse((self.source_dir / "Mixes").exists())
        self.assertTrue(self.source_dir.exists())

    def test_match_existing_album_dir_exact_and_fuzzy(self):
        artist_dir = self.music_dir / "Radiohead"
        artist_dir.mkdir()
        (artist_dir / "OK Computer (Collector's Edition)").mkdir()
        (artist_dir / "In Rainbows").mkdir()

        # Exact match
        res1 = org.match_existing_album_dir(artist_dir, "In Rainbows")
        self.assertEqual(res1.name, "In Rainbows")

        # Fuzzy match: OK Computer joins existing Collector's Edition
        res2 = org.match_existing_album_dir(artist_dir, "OK Computer")
        self.assertEqual(res2.name, "OK Computer (Collector's Edition)")

        # Fuzzy match: In Rainbows (Deluxe) joins In Rainbows
        res3 = org.match_existing_album_dir(artist_dir, "In Rainbows (Deluxe)")
        self.assertEqual(res3.name, "In Rainbows")

        # Non-matching album creates new path
        res4 = org.match_existing_album_dir(artist_dir, "Kid A")
        self.assertEqual(res4.name, "Kid A")

    def test_match_existing_dir_unicode_nfkc(self):
        # NFD on disk (like macOS APFS) vs NFC query
        import unicodedata
        nfd_name = unicodedata.normalize("NFD", "物語シリーズ")
        nfc_name = unicodedata.normalize("NFC", "物語シリーズ")
        (self.music_dir / nfd_name).mkdir()

        matched = org.match_existing_dir(self.music_dir, nfc_name)
        self.assertEqual(matched.name, nfd_name)
        self.assertTrue(matched.is_dir())

    def test_junk_album_re(self):
        # Watermarks, YouTube views, domains, charts should be flagged
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("【熊猫无损音乐 xmwsyy.com】更多打包资源下载")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("9755万回視聴")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("1.3 M de vistas")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("DailyTunez.com")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("Billboard Hot 100 Singles Chart")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("New Phonk Songs 2025 Playlist")))
        self.assertTrue(bool(org.JUNK_ALBUM_RE.search("YouTube")))

        # Legitimate album names should not be flagged
        self.assertFalse(bool(org.JUNK_ALBUM_RE.search("Scary Monsters and Nice Sprites")))
        self.assertFalse(bool(org.JUNK_ALBUM_RE.search("LOST CORNER")))
        self.assertFalse(bool(org.JUNK_ALBUM_RE.search("Discovery")))
        self.assertFalse(bool(org.JUNK_ALBUM_RE.search("Currents")))
        self.assertFalse(bool(org.JUNK_ALBUM_RE.search("10,000 gecs")))

    def test_plan_move_planned_albums_dedup(self):
        # Two tracks planned in the same run for the same artist
        song1 = self.source_dir / "Artist - Song One.mp3"
        song2 = self.source_dir / "Artist - Song Two.mp3"
        song1.write_bytes(b"data")
        song2.write_bytes(b"data")

        planned_dirs = {}
        planned_albums = {}

        plan1 = org.plan_move(song1, self.music_dir, planned_dirs=planned_dirs, planned_albums=planned_albums, resolved_album="Masterpiece (Deluxe)")
        plan2 = org.plan_move(song2, self.music_dir, planned_dirs=planned_dirs, planned_albums=planned_albums, resolved_album="Masterpiece")

        # Both songs should share the exact same album destination folder
        self.assertEqual(plan1.dest_audio.parent, plan2.dest_audio.parent)



class RunAllTests(unittest.TestCase):
    def test_run_all_main_no_steps_when_all_disabled(self):
        code = run_all.main(["--no-layout", "--no-art", "--no-artists", "--no-lyrics"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
