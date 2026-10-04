import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import library_layout as ll  # noqa: E402


def snapshot(root: Path) -> dict:
    """Every file under root -> content hash, plus every folder (so removed/created folders show up too)."""
    out = {}
    for cur, dnames, fnames in os.walk(root):
        for d in dnames:
            out[str(Path(cur, d).relative_to(root)) + "/"] = ""
        for f in fnames:
            out[str(Path(cur, f).relative_to(root))] = hashlib.sha1(Path(cur, f).read_bytes()).hexdigest()
    return out


class AlbumKeyTests(unittest.TestCase):
    """The exact pairs found in the real library: each pair must collapse to one key."""

    def same(self, a, b, artist):
        self.assertTrue(ll.album_key(a, artist))
        self.assertEqual(ll.album_key(a, artist), ll.album_key(b, artist), (a, b))

    def test_real_world_pairs(self):
        self.same("Ado's Best Adobum", "Ado’s Best Adobum", "Ado")                      # straight vs curly quote
        self.same("The Singles – The First Fifty Years", "The Singles - The First Fifty Years", "ABBA")  # dash types
        self.same("Ado - Show", "Show", "Ado")                                             # YouTube "Artist - " prefix
        self.same("Ado - Adoの歌ってみたアルバム", "Adoの歌ってみたアルバム", "Ado")
        self.same("Ado - UTA'S SONGS ONE PIECE FILM RED", "UTA'S SONGS ONE PIECE FILM RED", "Ado")
        self.same("SUGAR RUSH", "Sugar Rush - EP", "Aests")                                # case + "- EP"
        self.same("Timely", "TIMELY!!", "anri")                                            # case + punctuation
        self.same("Trunks (From Highest 2 Lowest)", "A$AP Rocky - Trunks (From _Highest 2 Lowest_)", "A$ap Rocky")
        self.same("REFRESH - Aests", "REFRESH", "Aests")                                   # trailing " - Artist"

    def test_different_albums_stay_different(self):
        self.assertNotEqual(ll.album_key("The Classics", "Aests"), ll.album_key("The Classics + Sugar Rush (Double EP)", "Aests"))
        self.assertNotEqual(ll.album_key("BULLY - DELUXE", "Kanye West"), ll.album_key("BULLY", "Kanye West"))
        self.assertNotEqual(ll.album_key("1000 gecs", "100gecs"), ll.album_key("10000gecs", "100gecs"))

    def test_names_with_nothing_to_compare_are_never_grouped(self):
        self.assertEqual(ll.album_key("🎧", "x"), "")
        self.assertTrue(ll.is_odd_name("🎧"))
        self.assertTrue(ll.is_odd_name("null"))
        self.assertFalse(ll.is_odd_name("レトベア"))   # non-Latin names are real names


class LayoutTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "Music"
        self.logs = base / "logs"
        self.checked = base / "lyrics_checked.json"
        for patcher in (mock.patch.object(ll, "LOG_DIR", self.logs), mock.patch.object(ll, "CHECKED_FILE", self.checked)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, files: dict):
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes) else content.encode())

    def library(self):
        self.make({
            "Ado/Show/01 a.flac": "a1", "Ado/Show/02 b.flac": "a2", "Ado/Show/04 d.flac": "a4", "Ado/Show/05 e.flac": "a5",
            "Ado/Show/cover.jpg": "same-cover",   # Show has 4 songs, Ado - Show 3: Show is the one kept
            "Ado/Ado - Show/02 b.flac": "a2",                      # identical copy of a song already in Show
            "Ado/Ado - Show/03 c.flac": "a3", "Ado/Ado - Show/03 c.lrc": "lyrics c", "Ado/Ado - Show/03 c.html": "page",
            "Ado/Ado - Show/cover.jpg": "same-cover",
            "Ado/Ado - Show/01 a.flac": "DIFFERENT",               # same name, different content: a conflict
            "Ado/Ado - Show/._03 c.flac": "junk",
            "Ado/Singles/x.mp3": "x",
            "A$ap Rocky/TESTING/t.mp3": "t", "A$AP Rocky/Other/o.mp3": "o",
            "Yuki Chiba, VALORANT/Singles/s.lrc.bak": "bak", "Yuki Chiba, VALORANT/Singles/s.mp3": "s",
            "Yuki Chiba, VALORANT/Singles/s.lrc": "lrc",
            "Lonely/Album/only.lrc.bak": "b", "Lonely/Album/._x": "j",
            "🎧/e.mp3": "e", "stray.txt": "stray", "._Vines": "j",
            "Artist Art/Ado.jpg": "pic", "Artist Art/._Ado.jpg": "j",
        })
        (self.root / "Empty/Deep").mkdir(parents=True)

    def root_ignores_case(self) -> bool:
        return (self.root / "ADO").exists() and (self.root / "ado").exists()

    def scan(self):
        return ll.scan(self.root, [self.root / "Artist Art"])


class ScanTests(LayoutTestCase):
    def test_finds_each_kind_of_mess(self):
        self.library()
        s = self.scan()
        names = lambda groups: sorted(g.folders[0].path.name + "|" + "|".join(f.path.name for f in g.folders[1:]) for g in groups)
        self.assertEqual(names(s.albums), ["Show|Ado - Show"])
        # macOS and Windows volumes ignore case, so there "A$AP Rocky" and "A$ap Rocky" are one folder
        self.assertEqual([sorted(f.path.name for f in g.folders) for g in s.artists],
                         [] if self.root_ignores_case() else [["A$AP Rocky", "A$ap Rocky"]])
        self.assertEqual([f.path.name for f in s.collabs], ["Yuki Chiba, VALORANT"])
        self.assertEqual([f.path.name for f in s.odd], ["🎧"])
        self.assertEqual([p.name for p in s.loose], ["stray.txt"])
        self.assertEqual(sorted(p.name for p in s.junk), ["._03 c.flac", "._Vines", "._x"])   # not Artist Art's: it's skipped
        self.assertEqual(sorted(p.name for p in s.backups), ["only.lrc.bak", "s.lrc.bak"])
        empty = [p.name for p in s.empty]
        self.assertEqual(sorted(empty), ["Album", "Deep", "Empty", "Lonely"])
        self.assertLess(empty.index("Deep"), empty.index("Empty"))                            # children before parents
        self.assertLess(empty.index("Album"), empty.index("Lonely"))

    def test_the_summary_line_pluralises_each_phrase(self):
        import io
        from contextlib import redirect_stdout
        self.library()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ll.print_report(self.scan())
        text = buf.getvalue()
        self.assertIn("1 duplicate album folder ", text)
        self.assertIn("2 .lrc.bak files", text)
        self.assertNotIn("ss ", text.split("songs")[-1])   # no "wayss" / "filess"

    def test_the_canonical_folder_is_the_one_with_the_most_songs(self):
        self.library()
        (group,) = self.scan().albums
        self.assertEqual(group.folders[0].path.name, "Show")   # 4 songs vs 3
        self.assertEqual([f.audio for f in group.folders], [4, 3])

    def test_a_tie_prefers_the_plain_name(self):
        self.make({"Ado/Show/1.flac": "1", "Ado/Ado - Show/2.flac": "2"})
        (group,) = self.scan().albums
        self.assertEqual(group.folders[0].path.name, "Show")

    def test_the_report_is_read_only_and_complete(self):
        self.library()
        before = snapshot(self.root)
        s = self.scan()
        lines = dict(ll.report_lines(s, full=True))
        self.assertTrue(any(t.startswith("Duplicate album folders") for t in lines))
        self.assertEqual(snapshot(self.root), before)


class CleanTests(LayoutTestCase):
    def test_a_preview_changes_nothing(self):
        self.library()
        before = snapshot(self.root)
        ll.do_clean(self.scan(), self.root.parent / "Backups", apply=False)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.logs.exists())

    def test_apply_deletes_junk_moves_backups_and_removes_empty_folders(self):
        self.library()
        backups = self.root.parent / "Backups"
        rec = ll.do_clean(self.scan(), backups, apply=True)
        s = self.scan()
        self.assertEqual((s.junk, s.backups), ([], []))
        self.assertFalse((self.root / "Empty").exists())
        self.assertFalse((self.root / "Lonely").exists())           # held only a backup and junk
        # the backup landed where the lyrics tool's lyrics.backup_dir mirror would look for it
        self.assertEqual((backups / "Yuki Chiba, VALORANT/Singles/s.lrc.bak").read_text(), "bak")
        self.assertTrue((self.root / "Yuki Chiba, VALORANT/Singles/s.lrc").is_file())   # the song's own files stay
        self.assertEqual(len([o for o in rec.ops if o["op"] == "move"]), 2)

    def test_without_a_backup_folder_the_backups_stay_put(self):
        self.library()
        ll.do_clean(self.scan(), None, apply=True)
        self.assertEqual(len(self.scan().backups), 2)
        self.assertTrue((self.root / "Lonely/Album/only.lrc.bak").is_file())   # so its folder isn't removed either


class MergeTests(LayoutTestCase):
    def test_a_preview_changes_nothing(self):
        self.library()
        before = snapshot(self.root)
        s = self.scan()
        ll.do_merge(s, s.albums, apply=False)
        self.assertEqual(snapshot(self.root), before)

    def test_artist_folders_spelled_two_ways_merge_into_the_fuller_one_and_undo(self):
        self.make({"100 gecs/1000 gecs/a.mp3": "a", "100 gecs/1000 gecs/b.mp3": "b", "100 gecs/1000 gecs/e.mp3": "e", "100gecs/1000 gecs/c.mp3": "c",
                   "100gecs/Singles/d.mp3": "d", "100gecs/Singles/d.lrc": "words"})
        before = snapshot(self.root)
        s = self.scan()
        (group,) = s.artists
        self.assertEqual(group.folders[0].path.name, "100 gecs")
        saved = ll.do_merge(s, s.artists, apply=True, what="artist").save()
        self.assertFalse((self.root / "100gecs").exists())
        for rel in ("1000 gecs/c.mp3", "Singles/d.mp3", "Singles/d.lrc"):
            self.assertTrue((self.root / "100 gecs" / rel).is_file(), rel)
        ll.do_undo(saved, apply=True)
        self.assertEqual(snapshot(self.root), before)

    def test_merge_moves_songs_with_their_files_and_never_overwrites(self):
        self.library()
        s = self.scan()
        keep = s.albums[0].folders[0].path
        ll.do_merge(s, s.albums, apply=True)
        show, other = self.root / "Ado/Show", self.root / "Ado/Ado - Show"
        self.assertEqual(keep, show)
        self.assertEqual((show / "03 c.flac").read_text(), "a3")
        self.assertEqual((show / "03 c.lrc").read_text(), "lyrics c")        # sidecars travel with the song
        self.assertEqual((show / "03 c.html").read_text(), "page")
        self.assertEqual((show / "01 a.flac").read_text(), "a1")             # kept, not overwritten
        self.assertEqual((other / "01 a.flac").read_text(), "DIFFERENT")     # the conflicting copy stays where it was
        self.assertFalse((other / "02 b.flac").exists())                     # identical copy dropped
        self.assertFalse((other / "cover.jpg").exists())
        self.assertTrue(other.is_dir())                                      # not empty, so not removed

    def test_a_clean_merge_removes_the_emptied_folder_and_updates_the_lyrics_memory(self):
        self.make({"Ado/Show/01.flac": "1", "Ado/Ado - Show/02.flac": "2", "Ado/Ado - Show/._02.flac": "j"})
        old = str(self.root / "Ado/Ado - Show/02.flac")
        self.checked.write_text(json.dumps({old: {"status": "notfound"}, "other": {"status": "english"}}))
        s = self.scan()
        ll.do_merge(s, s.albums, apply=True)
        self.assertFalse((self.root / "Ado/Ado - Show").exists())
        data = json.loads(self.checked.read_text())
        self.assertNotIn(old, data)
        self.assertEqual(data[str(self.root / "Ado/Show/02.flac")], {"status": "notfound"})
        self.assertIn("other", data)

    def test_apply_then_undo_restores_the_library_exactly(self):
        self.library()
        before = snapshot(self.root)
        self.checked.write_text(json.dumps({str(self.root / "Ado/Ado - Show/03 c.flac"): {"status": "notfound"}}))
        keys_before = self.checked.read_text()
        s = self.scan()
        rec = ll.do_merge(s, s.albums, apply=True)
        saved = rec.save()
        self.assertNotEqual(snapshot(self.root), before)
        ll.do_undo(saved, apply=True)
        after = snapshot(self.root)
        # junk deleted by the merge ("._03 c.flac") is junk by definition and isn't brought back
        before.pop("Ado/Ado - Show/._03 c.flac")
        self.assertEqual(after, before)
        self.assertEqual(json.loads(self.checked.read_text()), json.loads(keys_before))
        self.assertTrue(saved.with_name(saved.stem + ".undone.json").is_file())
        self.assertIsNone(ll.latest_manifest())   # an undone run can't be undone twice

    def test_undo_preview_changes_nothing(self):
        self.library()
        s = self.scan()
        saved = ll.do_merge(s, s.albums, apply=True).save()
        merged = snapshot(self.root)
        ll.do_undo(saved, apply=False)
        self.assertEqual(snapshot(self.root), merged)
        self.assertTrue(saved.is_file())


class SkipFolderTests(LayoutTestCase):
    def test_artist_pictures_and_backups_are_not_treated_as_artists(self):
        self.make({"Artist Art/Ado.jpg": "p", "Backups/x/y.lrc.bak": "b", "Real/Album/a.mp3": "a"})
        s = ll.scan(self.root, [self.root / "Artist Art", self.root / "Backups"])
        self.assertEqual([p.name for p in s.artist_dirs], ["Real"])
        self.assertEqual(s.backups, [])

    def test_configured_ignore_names_are_skipped(self):
        cfg = {"music_dir": str(self.root), "artist_art": {"output_dir": "Artist Art"}, "lyrics": {"backup_dir": ""},
               "layout": {"ignore": ["🎧"]}}
        self.assertIn(self.root / "🎧", ll.skip_folders(cfg))


if __name__ == "__main__":
    unittest.main()
