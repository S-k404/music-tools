import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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



class SafeMoveTests(unittest.TestCase):
    """Songs are never replaced, a half-done move is cleaned up, and names stay within what a disk allows."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)
        self.music = Path(self.temp_dir) / "Music"
        for name in ("Downloads", "Mixes"):
            (self.music / name).mkdir(parents=True)

    def song(self, folder, content, name="Ado - Song.mp3"):
        path = self.music / folder / name
        path.write_bytes(content)
        return path

    def meta(self):
        return {"artist": "Ado", "album": "Show", "title": "Song", "track": None}

    def test_two_different_files_with_one_name_do_not_pick_the_same_destination(self):
        first, second = self.song("Downloads", b"short"), self.song("Mixes", b"a much longer recording")
        claimed = set()
        plans = [org.plan_move(s, self.music, track_meta=self.meta(), claimed=claimed) for s in (first, second)]
        self.assertEqual([p.dest_audio.name for p in plans], ["Ado - Song.mp3", "Ado - Song (2).mp3"])
        for plan in plans:
            self.assertTrue(org.execute_plan(plan))
        folder = self.music / "Ado" / "Show"
        self.assertEqual(sorted(f.read_bytes() for f in folder.iterdir()), [b"a much longer recording", b"short"])

    def test_a_third_copy_keeps_counting_and_case_does_not_hide_a_collision(self):
        claimed = set()
        names = []
        for folder, name in (("Downloads", "Ado - Song.mp3"), ("Mixes", "ado - song.mp3"), ("Mixes", "Ado - Song.mp3")):
            src = self.song(folder, name.encode(), name=name)
            names.append(org.plan_move(src, self.music, track_meta=self.meta(), claimed=claimed).dest_audio.name.lower())
        self.assertEqual(len(set(names)), 3, names)

    def test_a_file_that_turns_up_after_planning_is_never_replaced(self):
        src = self.song("Downloads", b"mine")
        plan = org.plan_move(src, self.music, track_meta=self.meta())
        plan.dest_audio.parent.mkdir(parents=True)
        plan.dest_audio.write_bytes(b"someone else's")   # appears between planning and moving
        self.assertFalse(org.execute_plan(plan))
        self.assertIn("already exists", plan.error)
        self.assertEqual(plan.dest_audio.read_bytes(), b"someone else's")
        self.assertEqual(src.read_bytes(), b"mine")

    def test_a_lyrics_file_is_not_replaced_and_the_song_still_counts_as_moved(self):
        src = self.song("Downloads", b"audio")
        (self.music / "Downloads" / "Ado - Song.lrc").write_text("new lyrics")
        plan = org.plan_move(src, self.music, track_meta=self.meta())
        plan.dest_audio.parent.mkdir(parents=True)
        (plan.dest_audio.parent / "Ado - Song.lrc").write_text("old lyrics")
        self.assertTrue(org.execute_plan(plan))
        self.assertEqual((plan.dest_audio.parent / "Ado - Song.lrc").read_text(), "old lyrics")
        self.assertTrue((self.music / "Downloads" / "Ado - Song.lrc").exists())
        self.assertEqual(len(plan.warnings), 1)

    def test_a_copy_that_stops_half_way_leaves_the_song_where_it_was(self):
        src = self.song("Downloads", b"audio")
        plan = org.plan_move(src, self.music, track_meta=self.meta())

        def broken_move(a, b):
            Path(b).write_bytes(b"au")   # the start of a copy, then the disk gives up
            raise OSError(28, "No space left on device")

        with mock.patch.object(org.shutil, "move", side_effect=broken_move):
            self.assertFalse(org.execute_plan(plan))
        self.assertIn("No space", plan.error)
        self.assertEqual(src.read_bytes(), b"audio")
        self.assertFalse(plan.dest_audio.exists())

    def test_a_huge_tag_gives_a_folder_name_a_disk_accepts(self):
        for name in ("x" * 400, "歌" * 200):
            safe = org.sanitize_name(name)
            self.assertLessEqual(len(safe.encode("utf-8")), org.MAX_NAME_BYTES)
            self.assertTrue(safe)
        self.assertEqual(org.sanitize_name("Normal Name"), "Normal Name")

    def test_main_keeps_both_songs_and_reports_no_errors(self):
        self.song("Downloads", b"short")
        self.song("Mixes", b"a much longer recording")
        config = Path(self.temp_dir) / "c.toml"
        config.write_text(f"music_dir = {json.dumps(str(self.music))}\n", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(org, "start_log"), contextlib.redirect_stdout(out):
            code = org.main(["--config", str(config), "--no-auto-album", "--no-artist-art", "--no-progress"])
        self.assertEqual(code, 0, out.getvalue())
        kept = sorted(f.read_bytes() for f in self.music.rglob("*.mp3"))
        self.assertEqual(kept, [b"a much longer recording", b"short"])


class PlayerLayoutTests(unittest.TestCase):
    """What Jellyfin, Plex and Navidrome look for next to the songs."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)
        self.music = Path(self.temp_dir) / "Music"
        self.music.mkdir()

    def plan_for(self, rel, album="Album"):
        song = self.music / rel
        song.parent.mkdir(parents=True, exist_ok=True)
        song.write_bytes(b"x")
        meta = {"artist": "Artist", "album": album, "title": "T", "track": None}
        return org.plan_move(song, self.music, track_meta=meta)

    def test_songs_in_a_disc_folder_stay_there_instead_of_colliding_in_the_album_folder(self):
        for rel in ("Artist/Album/Disc 1/01 Intro.mp3", "Artist/Album/Disc 2/01 Intro.mp3", "Artist/Album/CD3/01 Intro.mp3"):
            with self.subTest(rel=rel):
                plan = self.plan_for(rel)
                self.assertTrue(plan.is_noop)
                self.assertEqual(plan.dest_audio, self.music / rel)

    def test_a_disc_folder_in_the_wrong_album_is_still_sorted_out(self):
        plan = self.plan_for("Artist/Other Album/Disc 1/01 Intro.mp3", album="Album")
        self.assertFalse(plan.is_noop)
        self.assertEqual(plan.dest_audio.parent.name, "Album")

    def test_other_subfolders_are_still_flattened_into_the_album_folder(self):
        plan = self.plan_for("Artist/Album/Random/01 Intro.mp3")
        self.assertFalse(plan.is_noop)
        self.assertEqual(plan.dest_audio, self.music / "Artist" / "Album" / "01 Intro.mp3")

    def test_the_artist_picture_is_saved_for_jellyfin_plex_and_navidrome(self):
        (self.music / "Artist").mkdir()
        art = self.music / "Artist Art"
        art.mkdir()
        (art / "Artist.jpg").write_bytes(b"picture")
        self.assertTrue(org.copy_jellyfin_artist_art(self.music, "Artist", art))
        for name in ("folder.jpg", "artist.jpg"):   # Navidrome only looks for artist.*, Jellyfin for folder.*
            self.assertEqual((self.music / "Artist" / name).read_bytes(), b"picture", name)

    def test_a_png_is_not_saved_under_a_jpg_name(self):
        (self.music / "Artist").mkdir()
        art = self.music / "Artist Art"
        art.mkdir()
        (art / "Artist.png").write_bytes(b"png")
        org.copy_jellyfin_artist_art(self.music, "Artist", art)
        self.assertTrue((self.music / "Artist" / "folder.png").is_file())
        self.assertTrue((self.music / "Artist" / "artist.png").is_file())
        self.assertFalse((self.music / "Artist" / "folder.jpg").exists())

    def test_a_picture_already_there_is_never_replaced(self):
        (self.music / "Artist").mkdir()
        (self.music / "Artist" / "artist.jpg").write_bytes(b"mine")
        art = self.music / "Artist Art"
        art.mkdir()
        (art / "Artist.jpg").write_bytes(b"other")
        self.assertFalse(org.copy_jellyfin_artist_art(self.music, "Artist", art))
        self.assertEqual((self.music / "Artist" / "artist.jpg").read_bytes(), b"mine")
        self.assertFalse((self.music / "Artist" / "folder.jpg").exists())


class SameAlbumTests(unittest.TestCase):
    """The organizer reuses a folder that `mt tidy` would call the same album, so the two stop undoing each other."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)
        self.artist = Path(self.temp_dir) / "Artist"
        self.artist.mkdir()

    def folder(self, name, songs=1):
        (self.artist / name).mkdir()
        for i in range(songs):
            (self.artist / name / f"{i}.mp3").write_bytes(b"x")

    def test_the_cases_from_a_real_library(self):
        for existing, tag in (("TIMELY!!", "Timely"), ("WE DON’T TRUST YOU", "WE DON'T TRUST YOU"),
                              ("SUGAR RUSH", "Sugar Rush - EP"), ("The Singles – The First Fifty Years",
                                                                 "The Singles - The First Fifty Years"),
                              ("Kawakiwoameku", "Kawaki wo Ameku"), ("Lazarus (Original Soundtrack)", "Lazarus")):
            with self.subTest(existing=existing):
                self.folder(existing)
                self.assertEqual(org.match_existing_album_dir(self.artist, tag).name, existing)
                shutil.rmtree(self.artist / existing)

    def test_the_fuller_folder_wins_when_two_qualify(self):
        self.folder("Timely", songs=1)
        self.folder("TIMELY!!", songs=5)
        self.assertEqual(org.match_existing_album_dir(self.artist, "Timely!").name, "TIMELY!!")   # not an exact match of either

    def test_an_exact_match_beats_an_equivalent_one(self):
        self.folder("TIMELY!!", songs=5)
        self.folder("Timely", songs=1)
        self.assertEqual(org.match_existing_album_dir(self.artist, "Timely").name, "Timely")

    def test_a_different_album_starts_its_own_folder(self):
        self.folder("TIMELY!!")
        self.assertEqual(org.match_existing_album_dir(self.artist, "Other Album").name, "Other Album")
        self.assertEqual(org.match_existing_album_dir(self.artist, ""), self.artist)   # no album: the artist folder itself

    def test_very_short_names_are_not_guessed_at(self):
        self.folder("XY")
        self.assertEqual(org.match_existing_album_dir(self.artist, "X.Y").name, "X.Y")

    def test_albums_planned_in_one_run_share_a_folder_in_either_spelling(self):
        music = Path(self.temp_dir)
        src = music / "YouTube"
        src.mkdir()
        planned_dirs, planned_albums = {}, {}
        dests = []
        for i, album in enumerate(("TIMELY!!", "Timely", "Timely - EP")):
            path = src / f"Artist - Song {i}.mp3"
            path.write_bytes(b"x")
            meta = {"artist": "Artist", "album": album, "title": f"Song {i}", "track": None}
            dests.append(org.plan_move(path, music, planned_dirs=planned_dirs, planned_albums=planned_albums,
                                       track_meta=meta).dest_audio.parent.name)
        self.assertEqual(len(set(dests)), 1, dests)


class OrganizeOneSongTests(unittest.TestCase):
    """`mt organize PATH` takes files as well as folders (the duplicate remover hands it just the songs it kept)."""

    def test_a_single_song_can_be_organized(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib"
            song = lib / "YouTube" / "Artist - Title.mp3"
            song.parent.mkdir(parents=True)
            song.write_bytes(b"")
            config = Path(d) / "config.toml"
            config.write_text(f'music_dir = "{lib}"\nsave_logs = false\n')
            with redirect_stdout(io.StringIO()):
                code = org.main([str(song), "--no-auto-album", "--no-progress", "--config", str(config)])
            self.assertEqual(code, 0)
            self.assertTrue((lib / "Artist" / "Singles" / "Artist - Title.mp3").is_file())
            self.assertFalse(song.exists())


class RunAllTests(unittest.TestCase):
    def test_run_all_main_no_steps_when_all_disabled(self):
        code = run_all.main(["--no-layout", "--no-art", "--no-artists", "--no-lyrics"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
