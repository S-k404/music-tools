import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import find_artist_art as artists  # noqa: E402


class HomesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_artist_folders_are_found_by_name_and_ambiguous_ones_left_out(self):
        (self.dir / "x").mkdir()
        ignores_case = (self.dir / "X").exists()
        (self.dir / "x").rmdir()
        if ignores_case:
            self.skipTest("this volume ignores case, so two spellings of one name can't both exist")
        for name in ("Radiohead", "A$ap Rocky", "A$AP Rocky", ".Trashes", "$RECYCLE.BIN", "Fred again.."):
            (self.dir / name).mkdir()
        (self.dir / "stray.txt").write_text("x", encoding="utf-8")
        homes = artists.artist_homes(self.dir)
        self.assertEqual(homes[artists.name_key("Radiohead")], self.dir / "Radiohead")
        self.assertEqual(homes[artists.name_key("fred AGAIN")], self.dir / "Fred again..")   # same key as the folder
        self.assertNotIn(artists.name_key("A$ap Rocky"), homes)    # two folders spell it alike: can't tell which
        self.assertEqual(sorted(homes.values()), [self.dir / "Fred again..", self.dir / "Radiohead"])
        self.assertEqual(artists.artist_homes(self.dir / "missing"), {})

    def test_a_picture_in_either_place_counts_only_when_homes_are_given(self):
        (self.dir / "Radiohead").mkdir()
        (self.dir / "Radiohead" / "artist.png").write_bytes(b"x")
        shared = self.dir / "Artist Art"
        shared.mkdir()
        (shared / "Muse.jpg").write_bytes(b"x")
        homes = artists.artist_homes(self.dir)
        self.assertTrue(artists.has_picture(shared, "Radiohead", homes))
        self.assertTrue(artists.has_picture(shared, "Muse", homes))
        self.assertFalse(artists.has_picture(shared, "Radiohead"))     # the default only looks in the shared folder
        self.assertTrue(artists.has_picture(shared, "Muse"))


class PlacementRunTests(unittest.TestCase):
    PIC = "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000-000000-80-0-0.jpg"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        buf = io.BytesIO()
        Image.new("RGB", (400, 400), "blue").save(buf, "JPEG")
        self.jpeg = buf.getvalue()
        names = ("Radiohead", "Muse")
        old = (artists.deezer_get, artists.download, artists.youtube_channels)
        artists.deezer_get = lambda path, **kw: {"data": [{"id": 1, "name": kw.get("q", ""), "nb_fan": 5, "link": "https://deezer/x",
                                                           "picture_xl": self.PIC}] if kw.get("q") in names else []}
        artists.download = lambda url: self.jpeg
        artists.youtube_channels = lambda name: []
        self.addCleanup(lambda: (setattr(artists, "deezer_get", old[0]), setattr(artists, "download", old[1]),
                                 setattr(artists, "youtube_channels", old[2])))
        self.lib = self.dir / "lib"
        for song in ("Radiohead/OK Computer/Radiohead - Creep.mp3", "Muse - Uprising.mp3"):   # Muse has no folder of its own
            (self.lib / song).parent.mkdir(parents=True, exist_ok=True)
            (self.lib / song).write_bytes(b"")
        self.config = self.dir / "c.toml"
        self.config.write_text(f'music_dir = {json.dumps(str(self.lib))}\nsave_logs = false\n', encoding="utf-8")

    def run_tool(self, *argv):
        old = sys.argv
        sys.argv = ["find_artist_art.py", "--config", str(self.config), "--no-progress", "--auto", *argv]
        try:
            artists.main()
        finally:
            sys.argv = old

    def test_artist_folder_placement_saves_artist_jpg_and_falls_back_to_the_shared_folder(self):
        self.run_tool("--placement", "artist_folder")
        with Image.open(self.lib / "Radiohead" / "artist.jpg") as img:
            self.assertEqual(img.size, (400, 400))
        self.assertTrue((self.lib / "Artist Art" / "Muse.jpg").is_file())          # no Muse folder: shared fallback
        self.assertFalse((self.lib / "Artist Art" / "Radiohead.jpg").exists())
        (self.lib / "Radiohead" / "artist.jpg").write_bytes(b"mine")
        self.run_tool("--placement", "artist_folder")                              # everyone already has one
        self.assertEqual((self.lib / "Radiohead" / "artist.jpg").read_bytes(), b"mine")

    def test_no_shared_folder_is_created_when_every_picture_has_a_home(self):
        (self.lib / "Muse").mkdir()
        self.run_tool("--placement", "artist_folder")
        self.assertTrue((self.lib / "Muse" / "artist.jpg").is_file())
        self.assertFalse((self.lib / "Artist Art").exists())

    def test_the_default_is_unchanged(self):
        self.run_tool()
        self.assertTrue((self.lib / "Artist Art" / "Radiohead.jpg").is_file())
        self.assertFalse((self.lib / "Radiohead" / "artist.jpg").exists())

    def test_the_setting_in_the_config_works_too(self):
        self.config.write_text(f'music_dir = {json.dumps(str(self.lib))}\nsave_logs = false\n[artist_art]\nplacement = "artist_folder"\n', encoding="utf-8")
        self.run_tool()
        self.assertTrue((self.lib / "Radiohead" / "artist.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
